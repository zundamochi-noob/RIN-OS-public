# STATE（最終更新: 2026-10-17）

## 現在の状態
- ブランチ `dev/llm-abstraction`。**`LLM_BACKEND = "anthropic"`**、モデルは `claude-haiku-5-5`。ollama に戻すときは `core/config.py` の1行。
- 安全策（Anthropic）: 月額上限 $2.5（`core/usage.py`）／max_tokens は上限400で丸める／タイムアウト30秒・リトライ1回。
  使用量は `LOGS_DIR\usage\anthropic-YYYY-MM.json` に月ごとに保存する（トークン数のみ・本文なし）。
  上限到達・集計を読めない（ストレージ未マウント等）・API失敗のときは、APIを呼ばず／本文なしのログで、固定メッセージを返す。ollamaへの自動フォールバックはしない。
  単価は公式の料金ページで確認（Haiku 5.5、10万トークン以下: 入力$0.10・出力$0.50 / MTok）。
- 外部APIへ送る情報: 日記（diary）は RAG の検索対象から除外している。会話ログ（conversation）は検索対象（当日分は除く）。
- **凛のキャラ設定（システムプロンプト）の実際の保存場所は `%USERPROFILE%\AppData\LocalLow\Shinymoon\MateEngineX\ZomeAI_prompt.txt`。**
  このファイルがあると、シーン（Prompt 欄）の値より優先される。
  **このファイルを削除してはいけない。** 削除すると、シーンにある重複した `AISystemPromptBinder` が空のファイルを作り、キャラ設定が空になる。
  旧版は `ZomeAI_prompt.txt.bak-20261017`、旧い会話履歴は `ZomeAI.json.bak-20261017` / `ZomeAI.json.bak-20261017b` に退避してある。
  反映後のリクエストかどうかは、`/completion` の構造ログ `[completion] ... system_chars` で見分けられる（旧版は667前後、新案は約910前後）。
  リポジトリの `SYSTEM_PROMPT`（`core/ollama.py`）は旧版のまま。`/input` 経路の整備のときに揃える。
- `/completion`（Mate Engine）: ChatML を system と messages に分解し、日付・表記ルール・RAGの記憶を system に足す。
  Warmup（n_predict=0、または分解後に空／最後がuserでない形）はLLMを呼ばず空を即返す。分解後の先頭は必ずuser。
  ログは構造のみ（件数・文字数）。prompt の生文字列を出す [TEMP] ログは削除済み。
- 表記ルール（`core/ollama.py` の `style_rules()`）は `/completion` の system にだけ足す（`/input` には入らない）。
- **会話の記録（`core/record.py`）**: `COMPLETION_RECORD = True`（`core/config.py`）のとき、`/completion` の返答を返したあとに、
  最新のuser発言と返答の1組だけを `conversations\YYYY-MM-DD.md` に追記し、インデックスする（書式は `/input` と同じ）。
  Warmup・フォールバック・固定メッセージ・空の返答・直近 `COMPLETION_DEDUPE_SEC`（30秒）以内の同じ発言（再送）は記録しない。
  失敗しても返答に影響せず、本文なしのログに1行だけ書く。
  注意: 記録した会話は、翌日以降のRAGで最大3件ずつsystemに入り、外部APIに送られる。
  会話の履歴の保管場所は3か所になる（凛の `history.json`、Unity の `ZomeAI.json`、`conversations`）。
- **読み上げ（`core/speech.py`）**: `COMPLETION_SPEAK = True` のとき、`/completion` の返答を別タスクで VOICEVOX で読み上げる
  （句点・改行で文に分け、再生中に次の文を合成。新しい返答が来たら古い返答の残りは捨てる。再生は直列）。
  音声OFF（`voice_enabled`）・Warmup・固定メッセージ・空の返答は読まない。失敗しても返答に影響しない。
  声は冥鳴ひまり（ノーマル、style id 14 = `SPEAKER_ID`。`/input` も同じ）。起動時に `GET /speakers` で照合し、違えばログに1行出す
  （VOICEVOX未起動ならスキップ）。出力は `AUDIO_DEVICE = None`（Windows の既定の出力。起動時点のものを使う）。
  音声パラメータは、音量 0.5（`VOICE_VOLUME_SCALE`）と抑揚 1.2（`VOICE_INTONATION_SCALE`）だけ上書きし、他は VOICEVOX の初期値（config の定数）。
- **起動**: `start_rin.bat` / `stop_rin.bat` / `restart_rin.bat`（下の「起動方法」）。順序は VOICEVOX → 凛（ポート待ち）→ Mate Engine。
  Mate Engine はビルドした exe（`MATE_ENGINE_EXE`）を起動する。
  別の場所の MateEngineX（Steam版など）が動いていれば警告して Mate Engine は起動しない。
  Windows の自動起動（HKCU\...\Run の MateEngine）は、2026-10-17 時点でビルド版の exe を指している（変更していない）。
- 方針: 凛のUIは Mate Engine(Unity) に統合する。`ui/overlay.py` は廃止予定のため触らない（`run.ps1` は overlay を起動しない）。

## Mate Engine のフォーク（別リポジトリ・ローカル管理）
- 公式 `2c5ea6b8`（2026-01-20）を土台に git 管理している（ローカルのみ。remote の `upstream` は公式で、push URL は `DISABLED` に設定済み。push 先はない）。
  公式の GitHub は 2026-01-20 以降更新がない。
- フォークの独自変更:
  - Main シーンの LLMCharacter の設定（Remote、host、port、stream、プロンプト）と、LLM コンポーネントの無効化。
  - `Assets\LLMUnity\Samples\ChatBot\ChatBot.cs` のデバッグ用ログ（`[DEBUG-RIN]`）と Warmup 用の try-catch（+16/−1。土台に戻して問題ない見込みだが未実施）。
  - それ以外は Unity の再保存による差分（ThemeManager の .mat、liltoon の .shader など。コミットしていない）。
- 待機アニメーションの差し替え（`eba111d1`）は試して取り消した（`4b7ff56a`）。
  つんつんの動きは `PET_IDLE`（Female の IdleIndex 0 と 19。`CUSTOM_DANCE` と `PET_IDLE 1` は同一内容）に含まれる。起動直後の `PET_INTRO.anim` に含まれるかは未確認。
- 歩行（Locomotion）・画面端に隠れる機能（AvatarHideHandler）は、公式の Update シーンにだけあり、Main シーンには入っていない。
  「隠す」の手動トグルやホットキーは、公式にもない（公式 issue #413 などに要望がある）。公式の更新を待つ方針。

## 成果物の場所
- `core/llm.py`（窓口）/ `llm_anthropic.py`（使用中）/ `llm_ollama.py`（残置。削除の判断は下記）/ `llm_bedrock.py`（骨組み・未検証）
- `core/chatml.py` … ChatML分解 / `core/context.py` … RAG検索の共通処理 / `core/usage.py` … 月次の使用量集計と上限
- `core/record.py` … 会話の記録 / `core/speech.py` … 読み上げ
- `scripts/anthropic_probe.py` … 疎通確認（2回だけ課金される）
- `tests/` … `python -m unittest discover -s tests`（使用量・会話・ChromaDB・VOICEVOX・音声デバイスは使わない。モックと一時フォルダ）

## 起動方法
- `start_rin.bat` … `run.ps1` を呼ぶ。VOICEVOX → 凛 → Mate Engine の順に起動する。
- `stop_rin.bat` … `stop.ps1` を呼ぶ。Mate Engine → 凛の順に止める。`stop_rin.bat -StopVoicevox` で VOICEVOX も止める。
- `restart_rin.bat` … `restart.ps1` を呼ぶ。Mate Engine・VOICEVOX も止めてから起動し直す。
- .bat を使う理由: `.ps1` はダブルクリックや実行ポリシーの制限で動かないことがあるため（ps1 が実行できない環境用）。.bat が `-ExecutionPolicy Bypass` をそのプロセスだけに付けて呼ぶ（ポリシー自体は変更しない）。
- 終了時に画面が閉じないよう `pause` する。タスクスケジューラーなどから呼ぶときは `/nopause` を付ける（終了コードは PowerShell のものをそのまま返す）。

## 拡張（後回し）
- `/input` 経路の整備: 声・天気・タイマー・会話の記録。あわせて本文を出すログ類
  （`main.py` の `[turn] input=`、`_producer` の `print(chunk)`、`route_tool` の `answer=`）の整理、リポジトリ側 `SYSTEM_PROMPT` の更新。
- 天気などのツール（Mate Engine の `/completion` 経路には未接続。公式タイマーの利用を含めて検討）。
- 声の作り込み（ElevenLabs や Qwen3-TTS）。
- リップシンク（公式待ち）。
- Bedrock。
- `main.py` 起動時の warmup は、anthropic の失敗（キー未設定・月額上限）でも成功扱いになる（`core/ollama.py` の `warmup`）。

## 数日使ってから判断
- `llm_ollama.py` と ollama モデルの削除（ollama serve を止めるかも含む）。

## 実機で未確認
- `/input` 経路の1ターンの通し（リファクタ後）。
- 月額上限に実際に達した状態の挙動（実装済み・単体テスト済み）。

## 実機確認済み
- 返答が冥鳴ひまりの声で鳴ること。
- 会話が `conversations\今日の日付.md` に追記され、翌日以降のRAGで使われること。
- `.bat`（`start_rin.bat` / `stop_rin.bat` / `restart_rin.bat`）での起動・停止。

## 次にやること
1. Unity を閉じてから、フォークの `ChatBot.cs` を土台に戻す（デバッグ用の変更のみのため）。
2. 上記「拡張」「数日使ってから判断」の順に、必要になったものから着手する。

## 公式ドキュメント調査メモ
- Anthropic API（claude-haiku-5-5）: 思考は既定でON。`thinking={"type":"disabled"}` でOFF（effortはhigh以下）。
  temperature/top_p/top_k は送るとエラーになりうる。assistantプリフィルはエラー（messagesは必ずuserで終える）。
  思考トークンは max_tokens に含まれる。応答は thinking ブロックで始まりうる（typeでtextを選ぶ）。
- Bedrock（Converse）: thinking は additionalModelRequestFields の {"thinking": {"type": "disabled"}}。effortは output_config.effort。
  交互ルール・boto3最低バージョンは公式で確認できず（不明。非公式情報は1.39.0以上）。
