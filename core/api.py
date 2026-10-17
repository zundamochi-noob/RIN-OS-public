from fastapi import FastAPI
from pydantic import BaseModel
import asyncio
import logging

from core import chatml, llm, record, speech
from core.config import REPLY_MAX_TOKENS
from core.context import search_memory, format_memory_context
from core.ollama import compose_system, memory_suffix, style_rules

logger = logging.getLogger("rin.api")

app = FastAPI()
input_queue = asyncio.Queue()
voice_enabled = True  # 音声ON/OFFフラグ


# Mate Engine（LLMUnity）から送られてくるリクエスト形式。
# prompt にはMate Engine側で組み立てたキャラ設定＋会話履歴＋直近発言が
# ChatML形式の1本の文字列としてまとめて入ってくる（分解はcore/chatml.py）。
# n_predict=0 はUnityのWarmup（結果を使わない事前読み込み）、-1 は本番の会話。
# temperature等のllama.cpp固有サンプリングパラメータは受け取らず、LLMにも転送しない。
class CompletionRequest(BaseModel):
    prompt: str
    stream: bool = False
    n_predict: int | None = None

# overlayからのテキスト入力を受け取るエンドポイント
@app.post("/input")
async def receive_input(data: dict):
    text = data.get("text", "")
    await input_queue.put(text)
    return {"status": "ok"}

# 音声ON/OFF切り替えエンドポイント
@app.post("/toggle_voice")
async def toggle_voice():
    global voice_enabled
    voice_enabled = not voice_enabled
    return {"voice_enabled": voice_enabled}


# Mate Engine（LLMUnityのRemoteモード）専用エンドポイント。
# キャラ設定・会話履歴の管理はMate Engine側（Prompt欄）に一本化している。
# ここではpromptをChatMLから system と messages に分解し、日付とRAGの記憶だけを足してLLMに渡す。
# route_tool・タイマーは使わない（タイマー発火はVOICEVOX/overlay側に出るためMate Engineには届かず、
# route_toolは毎ターンLLM呼び出しが1回増えるため）。
# デバッグ用printを仕込み、リクエスト受信・レスポンス返却の各段階が実際に到達しているかを可視化している。
@app.post("/completion")
async def completion(req: CompletionRequest):
    print("[DEBUG] /completion received, prompt len:", len(req.prompt), flush=True)
    content = await _complete_for_mate_engine(req)
    print("[DEBUG] /completion responding, content len:", len(content), flush=True)
    return {"content": content}


async def _complete_for_mate_engine(req: CompletionRequest) -> str:
    """promptを分解して生成する。ログは構造のみ（本文は書かない）。"""
    d = chatml.decompose(req.prompt)
    # Warmup: UnityはWarmupの戻りを使わない。n_predict=0、または分解後の最後がuserでない形なら、APIを呼ばない
    warmup = req.n_predict == 0 or d.status == "warmup"
    logger.info("[completion] n_predict=%s stream=%s warmup=%s %s", req.n_predict, req.stream, warmup, d.info)
    if warmup:
        return ""

    max_tokens = req.n_predict if req.n_predict and req.n_predict > 0 else REPLY_MAX_TOKENS

    if d.status == "fallback":
        # ChatMLとして解釈できない: 従来どおりprompt全体をuser1通で渡す
        return await llm.complete("", d.messages, max_tokens=max_tokens)

    # systemの末尾に足すもの: 表記のルール（常に）+ RAGの記憶（ヒットしたときだけ）。日付はcompose_systemが足す
    suffix_parts = [style_rules()]
    hits = []
    if d.latest_user:
        # RAGのクエリには、結合前の最新のuser発言を使う。RAGの失敗で会話自体は止めない
        try:
            hits = await search_memory(d.latest_user)
        except Exception as e:
            logger.warning("[completion] RAG検索に失敗したため記憶なしで続行: %s", type(e).__name__)
        if hits:
            suffix_parts.append(memory_suffix(format_memory_context(hits)))
    system = compose_system(d.system, "\n\n".join(suffix_parts))
    logger.info("[completion] rag_hits=%d system_chars=%d max_tokens=%d", len(hits), len(system), max_tokens)
    content = await llm.complete(system, d.messages, max_tokens=max_tokens)
    # 最新のuser発言と返答の1組だけを、バックグラウンドで記憶に記録する（失敗しても返答には影響しない）
    record.schedule_record(d.latest_user, content)
    # 返答を別タスクで読み上げる（音声OFFのときは読まない。失敗しても返答には影響しない）
    if voice_enabled:
        speech.schedule_speak(content)
    return content


# LLMUnityはRemoteモード初期化時に必ず/templateへ問い合わせる（LoadTemplate内のAskTemplate）。
# このエンドポイントが存在しないと接続確立処理が完了せず、Unity側がフリーズしたように
# 見える不具合の原因になっていた。
# 注意: UnityWebRequest.Put()で構築されるが、送信直前にrequest.method="POST"で
# 上書きされるため、実際に飛んでくるのはPOSTリクエスト（PUTではない）。
# 注意2: 空文字列を返すとLLMUnity側のChatTemplate.GetTemplate()が
# templates[""]という辞書アクセスでKeyNotFoundExceptionを投げてWarmup()が
# 静かに失敗する（Fire-and-forgetで呼ばれるため例外が表面化しない）。
# LLMUnity側に登録されている有効なテンプレート名（chatml等）を返す必要がある。
@app.post("/template")
async def template():
    return {"template": "chatml"}


class TokenizeRequest(BaseModel):
    content: str


# Warmup()実行時にInitNKeep()経由で呼ばれる。システムプロンプトの長さを
# 「トークン数」として把握するための呼び出し。凛側では正確なトークナイザを
# 持たないため、文字数をそのままダミーのトークンID列として返す近似実装にする
# （nKeepの値がズレても実害はほぼない）。
@app.post("/tokenize")
async def tokenize(req: TokenizeRequest):
    dummy_tokens = list(range(len(req.content)))
    return {"tokens": dummy_tokens}
