"""
凛OS 全体の設定を集約するモジュール。
モデル名・各種パス・ポート番号など、複数ファイルで使う値をここで一元管理する。
（旧構成ではモデル名やポートがollama.py・overlay.py・main.pyに散らばっていた）
"""
import os

# ---- パス関連 ----
# このファイル（config.py）は core/ にあるので、その一つ上がプロジェクトルート
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ChromaDBの永続化先
CHROMA_PATH = os.path.join(BASE_DIR, "data", "chroma")
# 短期会話履歴のJSON
HISTORY_PATH = os.path.join(BASE_DIR, "data", "history.json")

# ログ・日記・会話ログの保存先（サンプル値。自分の環境に合わせて変更する）
LOGS_DIR = r"C:\RinOS\Logs"
# run.ps1 が未マウントのときにマウントする VHD のパス（サンプル値）。VHD を使わないなら None
VHD_PATH = r"C:\RinOS\RinStorage.vhd"
DIARY_DIR = os.path.join(LOGS_DIR, "diary")
CONVERSATIONS_DIR = os.path.join(LOGS_DIR, "conversations")
RIN_LOG_PATH = os.path.join(LOGS_DIR, "rin.log")

# overlayから起動するmain.py本体と、それを動かすPython（overlayとは別バージョン）
MAIN_PY_PATH = os.path.join(BASE_DIR, "main.py")
MAIN_PY_PYTHON = os.path.expanduser(r"~\AppData\Local\Microsoft\WindowsApps\python3.12.exe")

# ---- モデル関連 ----
OLLAMA_URL = "http://127.0.0.1:11434"
MODEL = "qwen2.5:14b"
EMBED_MODEL = "intfloat/multilingual-e5-small"

# ---- LLMバックエンド ----
# 使うバックエンドの指定はここ1箇所だけ: "ollama" | "bedrock" | "anthropic"
LLM_BACKEND = "anthropic"
# 返答1回あたりの最大トークン数（/input経路と/completion経路で共通）
REPLY_MAX_TOKENS = 300
# Anthropic API直接利用（認証情報はコードに書かず、環境変数から読む）
ANTHROPIC_MODEL = "claude-haiku-5-5"
ANTHROPIC_API_KEY_ENV = "RIN_ANTHROPIC_API_KEY"
# 呼び出し側が大きなmax_tokensを渡しても、この値で丸める（llm_anthropic）
ANTHROPIC_MAX_TOKENS_CAP = 400
# SDKのタイムアウト（秒）とリトライ回数。リトライは最小限（失敗した1回分で最大2回の試行）
ANTHROPIC_TIMEOUT_SEC = 30
ANTHROPIC_MAX_RETRIES = 1
# 月額上限（USD）と単価（USD / 100万トークン）。単価は公式の料金ページで確認（2026-10-17、Claude Haiku 5.5）。
# プロンプトが10万トークン以下は標準、超える呼び出しは長文用の単価で換算する
ANTHROPIC_MONTHLY_BUDGET_USD = 2.5
ANTHROPIC_PRICE_PER_MTOK = {"input": 0.10, "output": 0.50}
ANTHROPIC_PRICE_PER_MTOK_LONG = {"input": 0.50, "output": 2.50}
ANTHROPIC_LONG_PROMPT_TOKENS = 100_000
# 使用量の月次集計の保存先（トークン数のみ。本文は保存しない）
USAGE_DIR = os.path.join(LOGS_DIR, "usage")
# Bedrock用（未検証の仮値）。認証情報はここに書かない。
# 環境変数 AWS_BEARER_TOKEN_BEDROCK をboto3が読む前提。
BEDROCK_REGION = "ap-southeast-2"
BEDROCK_MODEL_ID = "au.anthropic.claude-haiku-5-5"

# ---- VOICEVOX関連 ----
VOICEVOX_URL = "http://127.0.0.1:50021"
SPEAKER_ID = 14  # 冥鳴ひまり「ノーマル」（起動時に GET /speakers で名前と一致するか照合する）
SPEAKER_NAME = "冥鳴ひまり"
SPEAKER_STYLE_NAME = "ノーマル"
# 音声合成で上書きするパラメータはこの2つだけ。ほかは VOICEVOX の audio_query の初期値を使う
VOICE_VOLUME_SCALE = 0.5
VOICE_INTONATION_SCALE = 1.2
VOICEVOX_EXE = r"C:\Program Files\VOICEVOX\VOICEVOX.exe"  # サンプル値
# ビルドした Mate Engine（run.ps1 が凛の起動後に起動する。Steam版とは同時に動かさない）。サンプル値
MATE_ENGINE_EXE = r"C:\RinOS\MateEngine\MateEngineX.exe"
AUDIO_DEVICE = None     # 再生に使う出力デバイス。None は Windows の既定の出力（起動時点のもの）
SAMPLE_RATE = 44100    # 再生サンプルレート

# ---- サーバーポート ----
API_PORT = 8000        # FastAPI（overlay → 凛本体への入力）
OVERLAY_PORT = 8001    # 凛本体 → overlayへの返答表示

# ---- 会話設定 ----
HISTORY_LIMIT = 20     # 短期記憶として保持する会話履歴の件数

# ---- Mate Engine（/completion）の会話の記録 ----
# 最新のuser発言と返答の1組を conversations\YYYY-MM-DD.md に追記し、RAGの記憶としてインデックスする。
# 記録した会話は、次回以降のRAGで最大3件ずつsystemに入り、外部APIに送られる（当日の会話はRAGの対象外）。
COMPLETION_RECORD = True
# Unityの再送などで、同じ最新のuser発言をこの秒数以内に記録済みなら、記録しない
COMPLETION_DEDUPE_SEC = 30

# ---- Mate Engine（/completion）の返答の読み上げ ----
# 返答を返したあとに、別タスクでVOICEVOXで読み上げる（False なら読み上げない）
COMPLETION_SPEAK = True

# ---- 位置（天気取得用）----
LATITUDE = 34.74681423125429
LONGITUDE = 135.52035686706952
