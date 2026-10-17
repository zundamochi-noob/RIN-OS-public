# 凛OS 再起動スクリプト: run.ps1 を -RestartServices 付きで呼ぶ（Mate Engine と VOICEVOX も止めてから起動し直す）
# main.py の停止（APIポートの待ち受けプロセスのみ）や、ollama を起動するかの判断は run.ps1 が行う。
# 以前のように python* を一括で止めることはしない（無関係なプロセスを巻き込まないため）。
& "$PSScriptRoot\run.ps1" -RestartServices
exit $LASTEXITCODE
