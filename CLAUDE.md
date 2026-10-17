# RIN-OS — Claude Code向け作業メモ

## プロジェクト概要
凛OS（RIN-OS）。ホームOSアシスタント「凛」。詳細な構成はREADME.md参照。
キャラクター設定: 落ち着いた語調、標準語・ですます調、マークダウン/箇条書き禁止（話し言葉指示）、過剰な締め括り文句禁止。

## 実行環境の注意点

- **PowerShell**: `&&` は使用不可。コマンドはセミコロン区切り。終了コード確認は `echo "EXIT=$LASTEXITCODE"` を別行で実行する。
- **ログ保存先（VHD上のストレージ）**: `core/config.py` の `VHD_PATH` の VHD を `run.ps1` がマウントして使う。未マウント状態で凛を起動するとログ書き込みが個別にtry/exceptで握り潰され、エラーメッセージなしに全ログが失われる（既知の問題）。

## 検証・作業フロー

1. ファイルの現在の状態を確認（read_file / view）
2. 変更を加える
3. `py_compile` で構文検証
4. 動作確認（run.ps1経由で再起動し実際に触る）
5. コミット（頻度高めに。編集ツールの不安定性によるロス防止のため）

編集ツールが不安定な場合、ファイルが無声でトランケートされることがある。複数ファイルを編集したセッション後は行数比較（`git show HEAD` vs `Get-Content`）で検証すること。

## 実装済み機能メモ

### タイマー機能（2025-07 実装済み、main マージ済み）
- `core/timer.py`: `TimerManager` + `format_duration()`。`asyncio.create_task` で複数タイマーを並行管理
- `route_tool` が `tuple[str, dict] | None` を返すよう変更済み（旧 `str | None`）。1推論でツール判定+パラメータ抽出を同時実行。num_predict=15
- `_speaking_lock`（asyncio.Lock）で `_handle_turn` と `_fire_timer_notification` の音声衝突を防止
- タイマー発火時は会話履歴に `[システム: X分のタイマーが完了しました]` をuser/assistantペアで記録。これがないと「タイマー動いてる？」に誤答する
- タイマーは**サイドエフェクトツール**（データ返却なし）なので `TOOL_FUNCTIONS` に入れず `_handle_turn` 内で直接処理している

### ツール追加手順（weatherやtimerと同じパターン）
1. `ollama.py` の `TOOL_REGISTRY` に1行追加
2. コンテキストツール（データ取得型）なら `main.py` の `TOOL_FUNCTIONS` に追加
3. サイドエフェクトツール（副作用型）なら `_handle_turn` に `elif tool_name == "xxx":` を追加

### キャラクター設計の注意点
- システムプロンプトは「AIアシスタント」ではなく「じゅんの同居人のようなAI」と定義している。「アシスタント」という語はモデルの使命感バイアスを呼ぶため避ける
- LLMへのトリガーメッセージは会話らしい文体にする（例: `[タイマー完了]` → `（タイマーが鳴りました）`）。システム的な角括弧記法は通常会話と異なるモードに入りやすい

## 既知の技術的注意点

- **ChromaDB埋め込み**: `intfloat/multilingual-e5-small` 使用時は `query:` / `passage:` プレフィックスが検索精度に必須。ChromaDBの標準EmbeddingFunctionはバイパスし、`_embed_query()` / `_embed_passages()` を手動実装している。
- **HF_HUB_OFFLINE=1**: rag.py冒頭でモジュールインポート時に無条件セットされる。EMBED_MODELを未キャッシュの新モデルに変更すると、原因不明のOSError（offline mode由来）でクラッシュする。変更時は事前にモデルをダウンロードしておくこと。
- **WebSocket化の判断基準**: 複数クライアントへの同時配信（Discord連携など）が必要になるまでは現状のHTTP POSTで十分。保留中の設計。
