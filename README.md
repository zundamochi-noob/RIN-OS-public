# RIN-OS（凛）

個人用AIアシスタント「凛」。Anthropic API（claude-haiku-5-5）で会話し、VOICEVOXで読み上げ、ChromaDBによるRAGで過去の会話を記憶する。UIは Mate Engine（Unity）と連携する。
現状、Mate Engine から `/completion` 経由の会話、Anthropic API への接続、冥鳴ひまりの声での読み上げ、会話の記録と RAG での利用、`start_rin.bat` などの .bat での起動は、実機で動作を確認している。API の月額上限は実装済み・単体テスト済みで、実際に上限へ達した状態での実機確認は未了。
起動は Windows 前提。環境変数 `RIN_ANTHROPIC_API_KEY` を設定し、`core/config.py` のパスを自分の環境に合わせてから `start_rin.bat` を実行する（VOICEVOX → 凛（FastAPI, port 8000）→ Mate Engine の順に起動）。
Mate Engine 側のプロジェクトはこのリポジトリに含まない。
