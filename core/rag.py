import os

# HFキャッシュ済みモデルを使う場合、起動時のバージョン確認通信（HEAD多数）を
# スキップしてローカルキャッシュを直接読みにいく。常駐ホームOSとしては
# 外部通信への依存を減らしたいため既定でオフライン化する。
# 注意: EMBED_MODELを未キャッシュの別モデルに変更する場合は、
# 環境変数 HF_HUB_OFFLINE=0 を外部から設定して一時的にオンラインへ戻すこと
# （setdefaultなので既に外部で設定されている値は上書きしない）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import re
import logging
from datetime import datetime
import chromadb
from sentence_transformers import SentenceTransformer

from core.config import CHROMA_PATH, EMBED_MODEL

logger = logging.getLogger(__name__)

# ChromaDBのコレクション名（rag内部だけで使う識別子なのでconfigには出さない）
COLLECTION_NAME = "rin_memory"

# 検証済み: multilingual-e5-smallをCPU固定で使う（VRAM消費ほぼゼロ）。
# all-MiniLM-L6-v2より日本語の意味的な近さの分離精度が高いことを実測で確認済み。
# e5系モデルはquery:/passage:プレフィックスを付けないと精度が大きく落ちるため、
# ChromaDBのEmbeddingFunctionに任せず、ベクトル化を自前で行う設計にする
# （ChromaDB側はクエリ用とドキュメント用のテキストを区別する仕組みを持たないため）。
_model = None

_client = None
_collection = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        logger.info("[rag] embeddingモデルを読み込みます: %s (HF_HUB_OFFLINE=%s)",
                    EMBED_MODEL, os.environ.get("HF_HUB_OFFLINE", "0"))
        _model = SentenceTransformer(EMBED_MODEL, device="cpu")
    return _model


def _embed_passages(texts: list) -> list:
    """ドキュメント（保存対象）用のベクトル化。"passage: "プレフィックスを付ける"""
    model = _get_model()
    prefixed = [f"passage: {t}" for t in texts]
    return model.encode(prefixed, normalize_embeddings=True).tolist()


def _embed_query(text: str) -> list:
    """検索クエリ用のベクトル化。"query: "プレフィックスを付ける"""
    model = _get_model()
    return model.encode([f"query: {text}"], normalize_embeddings=True).tolist()[0]


def get_collection():
    """ChromaDBの永続クライアントとコレクションを取得（シングルトン）。
    embedding_functionは渡さない（ベクトル化は自前で行うため）。"""
    global _client, _collection
    if _collection is None:
        os.makedirs(CHROMA_PATH, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
        _collection = _client.get_or_create_collection(name=COLLECTION_NAME)
    return _collection


def _relative_day(date_str: str) -> str:
    """"2026-06-29"のような日付を「今日/昨日/N日前」の相対表現に変換する。
    RAG検索結果の前置ラベルに使い、LLMが記憶の時系列を混同しないようにする。"""
    if not date_str:
        return "いつか"
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return date_str
    delta = (datetime.now().date() - d).days
    if delta == 0:
        return "今日"
    if delta == 1:
        return "昨日"
    if delta < 0:
        return f"{-delta}日後"
    return f"{delta}日前"


# "## [14:32] User" や "## [14:32] 凛" の見出しで分割する（会話ログ用）
_HEADER_PATTERN = re.compile(r"^## \[(\d{2}:\d{2})\] (.+)$", re.MULTILINE)


def _chunk_markdown(text: str, source_date: str):
    """
    会話ログMarkdownを見出し（## [時刻] 話者）単位でチャンクに分割する。
    戻り値: [{"id": ..., "text": ..., "time": ..., "speaker": ...}, ...]
    """
    matches = list(_HEADER_PATTERN.finditer(text))
    chunks = []
    for i, m in enumerate(matches):
        time_str, speaker = m.group(1), m.group(2)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        chunk_id = f"conv_{source_date}_{time_str}_{speaker}_{i}"
        chunks.append({
            "id": chunk_id,
            "text": f"[{time_str}] {speaker}: {body}",
            "time": time_str,
            "speaker": speaker,
            "date": source_date,
        })
    return chunks


def _chunk_diary(text: str, source_date: str):
    """
    日記Markdownを段落（空行区切り）単位でチャンクに分割する。
    見出しの有無に関係なく機能する、自由記述向けの分割方式。
    戻り値: [{"id": ..., "text": ...}, ...]
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    for i, para in enumerate(paragraphs):
        # "# 2026-06-29" のような日付見出しだけの段落はスキップ
        if re.fullmatch(r"#+\s*\d{4}-\d{2}-\d{2}\s*", para):
            continue
        chunk_id = f"diary_{source_date}_{i}"
        chunks.append({
            "id": chunk_id,
            "text": para,
            "date": source_date,
        })
    return chunks


def index_markdown_file(filepath: str) -> int:
    """
    1つの会話ログMarkdownファイルを読み込み、チャンク分割してDBに登録する。
    差分インデックス: 既にDBにあるIDはスキップし、新規チャンクだけをエンベディングする。
    会話ログは追記のみで過去が書き換わらないため、ID一致＝登録済みとみなして安全。
    戻り値: 新たにインデックスしたチャンク数
    """
    if not os.path.exists(filepath):
        return 0

    filename = os.path.basename(filepath)
    source_date = os.path.splitext(filename)[0]  # "2026-06-29.md" -> "2026-06-29"

    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = _chunk_markdown(text, source_date)
    if not chunks:
        return 0

    collection = get_collection()
    # 既存IDを引いて、まだDBにないチャンクだけに絞る（再エンベディングを避ける）
    existing = set(collection.get(ids=[c["id"] for c in chunks])["ids"])
    new_chunks = [c for c in chunks if c["id"] not in existing]
    if not new_chunks:
        return 0

    texts = [c["text"] for c in new_chunks]
    collection.upsert(
        ids=[c["id"] for c in new_chunks],
        embeddings=_embed_passages(texts),
        documents=texts,
        metadatas=[
            {
                "date": c["date"],
                "time": c["time"],
                "speaker": c["speaker"],
                "source_type": "conversation",
            }
            for c in new_chunks
        ],
    )
    logger.debug("index_markdown_file: %s -> %d new chunks", filename, len(new_chunks))
    return len(new_chunks)


def index_diary_file(filepath: str) -> int:
    """
    1つの日記Markdownファイルを読み込み、段落単位でチャンク分割してDBにupsertする。
    日記は同日中に加筆・修正されうるため、差分スキップせずupsertで常に最新に保つ。
    戻り値: インデックスしたチャンク数
    """
    if not os.path.exists(filepath):
        return 0

    filename = os.path.basename(filepath)
    source_date = os.path.splitext(filename)[0]

    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = _chunk_diary(text, source_date)
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]
    collection = get_collection()
    collection.upsert(
        ids=[c["id"] for c in chunks],
        embeddings=_embed_passages(texts),
        documents=texts,
        metadatas=[
            {"date": c["date"], "source_type": "diary"}
            for c in chunks
        ],
    )
    logger.debug("index_diary_file: %s -> %d chunks", filename, len(chunks))
    return len(chunks)


def query(text: str, n_results: int = 3, exclude_source_types: set | None = None) -> list:
    """
    クエリ文に近い過去の記憶チャンクを検索する。
    - 会話ログ（conversation）：今日のものはhistory（短期記憶）が担当するため除外
    - 日記（diary）：当日に書いたものもRAG対象に含める
    - exclude_source_types: 指定したsource_type（"diary"など）を結果から除く。
      外部API（ローカル外）へ送るバックエンドで日記を送信対象から外すために使う。
      除外はPython側で取得後に行うため、除外対象が上位を占めると返る件数がn_resultsより減ることがある
    戻り値: [{"text": ..., "date": ..., "source_type": ..., "time": ..., "speaker": ...}, ...]
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    today = datetime.now().strftime("%Y-%m-%d")

    # 会話ログは今日以外、日記は全期間が対象になるOR条件
    # ChromaDBの$or+$andネストが不安定なため、Python側でフィルタリングする
    # where条件なしで多めに取得してからフィルタリング
    fetch_n = max(n_results * 4, 20)
    result = collection.query(
        query_embeddings=[_embed_query(text)],
        n_results=min(fetch_n, collection.count()),
    )

    hits = []
    docs = result.get("documents", [[]])[0]
    metas = result.get("metadatas", [[]])[0]
    distances = result.get("distances", [[]])[0]

    # ログは構造のみ（クエリ文や記憶の本文は書かない。外部APIへ送る可能性のある個人的な内容のため）
    logger.debug("query: chars=%d / total in DB: %d / today: %s",
                 len(text), collection.count(), today)

    for doc, meta, dist in zip(docs, metas, distances):
        source_type = meta.get("source_type")
        date = meta.get("date")
        if exclude_source_types and source_type in exclude_source_types:
            continue
        # 会話ログは今日以外、日記は全期間OK
        if source_type == "conversation" and date == today:
            continue
        # LLMが「いつの記憶か」を混同しないよう、相対日付（昨日・N日前）を前置する
        when = _relative_day(date)
        label = "日記" if source_type == "diary" else "会話"
        display_text = f"（{when}の{label}）{doc}"
        hits.append({
            "text": display_text,
            "date": date,
            "source_type": source_type,
            "time": meta.get("time"),
            "speaker": meta.get("speaker"),
        })
        if len(hits) >= n_results:
            break

    logger.debug("hits: %d件", len(hits))
    for h in hits:
        logger.debug("  source=%s date=%s chars=%d",
                     h["source_type"], h["date"], len(h["text"]))

    return hits
