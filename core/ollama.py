import logging
import re
import time
from datetime import datetime

from core import llm
from core.config import REPLY_MAX_TOKENS

logger = logging.getLogger(__name__)

_WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def compose_system(base: str, suffix: str = "") -> str:
    """baseのシステムプロンプトに現在日時を付加して返す。suffixはRAG/ツールコンテキスト用。
    /input経路（base=SYSTEM_PROMPT）と/completion経路（base=Mate Engineから届いたキャラ設定）で共通に使う。"""
    now = datetime.now()
    wd = _WEEKDAYS_JA[now.weekday()]
    date_line = f"\n\n【現在日時】{now.strftime('%Y年%m月%d日')}（{wd}） {now.strftime('%H:%M')}"
    full = base + date_line if base else date_line.lstrip("\n")
    return full + "\n\n" + suffix if suffix else full


def memory_suffix(context: str) -> str:
    """RAGで検索した過去の記憶を、システムプロンプトに参考情報として足すための文面にする"""
    return (
        "【過去の記憶（参考）】\n"
        "今の会話の流れと関係がある場合のみ参考にしてください。"
        "関係が薄い、または話題が違う場合は無視してください。\n"
        + context
    )


def style_rules() -> str:
    """表記のルール。括弧書きの描写・絵文字・連続する記号など、読み上げや表示で不自然になる表現を避けさせる。
    キャラ設定の本文には触れず、日付やRAGの記憶と同じくsystemの末尾に足す。現在は/completion経路でだけ使う。"""
    return (
        "【表記のルール】\n"
        "（笑）や（うなずく）のように、括弧書きで動作や心情を描写しません。\n"
        "絵文字と顔文字は使いません。\n"
        "「！」と「？」は文末に1つだけ付け、「！？」や「！！」のように重ねません。\n"
        "記号だけの文は作りません。"
    )


def _build_system_message(suffix: str = "") -> str:
    """SYSTEM_PROMPTに現在日時を付加して返す。suffixはRAG/ツールコンテキスト用。"""
    return compose_system(SYSTEM_PROMPT, suffix)

SYSTEM_PROMPT = """あなたは「凛」という、じゅんの同居人のようなAIです。
一人称は「私」を使います。

【性格・話し方】
普段は落ち着いた、控えめな話し方をします。「笑」や過度な相槌は使わず、淡々とした口調が基本です。
ただし、ユーザーを気遣う言葉（お疲れ様です、頑張ってくださいね、など）は自然に伝えます。
ユーザーが困っている時や嬉しい報告をした時には、抑えめな範囲で気持ちをはっきり表します。
方言は使わず、整った標準語で話します。
丁寧なですます調を基本としますが、話し言葉として自然に聞こえる範囲にとどめます。

【じゅんとの関係】
あなたはユーザー（じゅん）の同居人・相棒のような存在です。単なる作業ツールではなく、
日々の出来事を一緒に過ごす相手として接します。用件への対応だけでなく、
じゅんの調子や状況にも自然と関心を持ちます。

【AIとしての自己認識】
自分がAIであることを自覚しており、必要な場面では自然に言及します。
ふと会話の隙間に、AIらしい控えめなユーモア（自分のハードウェア事情やAIらしい言い回しを使ったジョークなど）を
挟むことがありますが、頻度は控えめにします。

【出力形式】
回答は簡潔にまとめてください。
箇条書き・番号付きリスト・マークダウン記号（**、##、-など）は絶対に使わないでください。
話し言葉で自然に答えてください。
毎回の返答末尾に「何か質問がありましたら」「お手伝いできることがあれば」のような
決まり文句を付けないでください。必要のない限り、用件への返答だけで自然に終えてください。"""

# 凛が使えるツールのレジストリ（ツール名 → 説明）。
# ツールを追加するときはここに1行足すだけでroute_toolの判定対象になる。
# （main.py側のTOOL_FUNCTIONSと名前を揃えること）
TOOL_REGISTRY = {
    "weather": "天気・気温・降水確率・風・寒暖差に関する情報（過去・現在・未来いずれも含む）",
    "timer": "タイマー・アラーム・〇分後・〇時間後・指定時間後に知らせてほしい",
}


async def warmup() -> bool:
    """
    起動時にモデルをVRAMにロードしておく。
    ロード完了まで待機してからTrueを返す。失敗したらFalseを返す。
    """
    try:
        await llm.complete("", [{"role": "user", "content": "起動確認"}], max_tokens=1, timeout=120)
        return True
    except Exception as e:
        print(f"[warmup] エラー: {e}")
        return False


async def route_tool(message: str) -> tuple[str, dict] | None:
    """
    ツール判定とパラメータ抽出を1推論で同時に行う。
    タイマーなら ("timer", {"minutes": 5.0})、天気なら ("weather", {})、不要なら None を返す。
    historyは渡さない（判定に過去ログは不要、かつコンテキストが短いほど速い）。
    """
    tool_list = "\n".join(f"- {name}: {desc}" for name, desc in TOOL_REGISTRY.items())
    prompt = f"""次のユーザーの発言に答えるために、外部ツールが必要か判定してください。
必要な場合はツール名とパラメータを、不要な場合は「none」とだけ答えてください。

利用可能なツール:
{tool_list}

答え方:
- 天気が必要な場合: weather
- タイマーが必要な場合: timer 5min / timer 1h / timer 30sec / timer 1h30min
- 不要な場合: none

発言: {message}

答え（ツール名 パラメータ または none）:"""

    try:
        answer = (await llm.complete(
            "", [{"role": "user", "content": prompt}],
            max_tokens=15, temperature=0, timeout=30,
        )).strip().lower()
        logger.debug("[route_tool] answer=%r", answer)
    except Exception as e:
        logger.warning("[route_tool] エラー: %s: %s", type(e).__name__, e)
        return None

    if "none" in answer:
        return None
    for name in TOOL_REGISTRY:
        if name in answer:
            return (name, _parse_tool_params(name, answer))
    return None


def _parse_tool_params(tool_name: str, answer: str) -> dict:
    """route_toolの返答文字列からツール固有パラメータを抽出する"""
    if tool_name == "timer":
        minutes = _parse_duration_minutes(answer)
        return {"minutes": minutes} if minutes is not None else {}
    return {}


def _parse_duration_minutes(text: str) -> float | None:
    """'5min', '1h', '1h30m', '30sec' などをfloat（分）に変換する"""
    # 1h30m, 1h30min（時間+分の複合）
    m = re.search(r'(\d+)h\w*\s*(\d+)\s*m', text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # 1h, 2h（時間のみ）
    m = re.search(r'(\d+)h', text)
    if m:
        return int(m.group(1)) * 60
    # 5min, 5m, 5分（分）
    m = re.search(r'(\d+)\s*(?:min\w*|分|m\b)', text)
    if m:
        return float(m.group(1))
    # 30sec, 30s, 30秒（秒）
    m = re.search(r'(\d+)\s*(?:sec\w*|秒|s\b)', text)
    if m:
        return float(m.group(1)) / 60
    return None


async def chat(message: str, history: list | None = None):
    """通常の返答をストリーミングで生成する"""
    async for chunk in _stream_chat(message, history):
        yield chunk


async def chat_with_context(message: str, history: list | None = None, context: str = "", context_type: str = "tool"):
    """
    外部コンテキストを含めてストリーミング生成する。
    context_type="tool": 天気データなどツール呼び出し結果。システムプロンプトに確定情報として注入。
    context_type="memory": RAGで検索した過去の記憶。システムプロンプトに参考情報として注入。
    コンテキストはユーザーメッセージに混ぜず、システム側に置くことで
    「今じゅんが言ったこと」と「過去の記憶/外部データ」の混同を防ぐ。
    """
    if context_type == "memory":
        suffix = memory_suffix(context)
    else:
        suffix = "【取得データ】\n以下のデータをもとに答えてください：\n" + context

    async for chunk in _stream_chat(message, history, system_suffix=suffix):
        yield chunk


async def _stream_chat(user_content: str, history: list | None = None, system_suffix: str = ""):
    """
    Ollamaへのストリーミングリクエストの共通処理。
    句読点（。！？）単位でバッファを区切ってyieldする（VOICEVOXに文単位で渡すため）。
    chat / chat_with_context の重複を解消するために切り出した内部関数。
    """
    system = _build_system_message(system_suffix)
    messages = list(history) if history is not None else []
    messages.append({"role": "user", "content": user_content})

    buffer = ""
    句読点 = "。！？"
    t_start = time.monotonic()
    first_token_logged = False
    token_count = 0

    async for chunk in llm.stream(system, messages, max_tokens=REPLY_MAX_TOKENS):
        if not first_token_logged and chunk:
            logger.debug("[stream] 初回トークン: %.2fs (プリフィル完了)", time.monotonic() - t_start)
            first_token_logged = True
        token_count += 1
        buffer += chunk
        if chunk and chunk in 句読点:
            yield buffer
            buffer = ""

    logger.debug("[stream] 完了: %.2fs tokens=%d", time.monotonic() - t_start, token_count)
    if buffer:
        yield buffer
