# 凛OS 起動スクリプト（overlay なし。main.py を単体で起動する）
#   使い方: powershell -ExecutionPolicy Bypass -File run.ps1
#   起動順: VOICEVOX → 凛（main.py。ポートの待ち受けを待つ）→ Mate Engine（core/config.py の MATE_ENGINE_EXE）
#   -RestartServices: Mate Engine と VOICEVOX（と、LLM_BACKEND が ollama のときは ollama）も止めてから起動し直す（restart.ps1 から使う）
# 設定値（LLM_BACKEND・ポート・パス）は core/config.py から読む。このファイルには書き写さない。
param([switch]$RestartServices)

$Root = $PSScriptRoot

function Fail([string]$Message) {
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Test-PortListening([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-Port([int]$Port, [int]$TimeoutSec, [bool]$Listening) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if ((Test-PortListening $Port) -eq $Listening) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return ((Test-PortListening $Port) -eq $Listening)
}

function Test-SamePath([string]$A, [string]$B) {
    if ([string]::IsNullOrWhiteSpace($A) -or [string]::IsNullOrWhiteSpace($B)) { return $false }
    return ([IO.Path]::GetFullPath($A) -ieq [IO.Path]::GetFullPath($B))
}

# 凛の待ち受けを確認できたあとに、ビルドした Mate Engine を起動する。起動しない場合は理由を表示する（凛の起動結果には影響しない）
function Start-MateEngine {
    $exe = $cfg.mate_engine_exe
    if (-not (Test-Path $exe)) {
        Write-Host "警告: Mate Engine の実行ファイルが見つかりません: $exe（Mate Engine は起動しません）" -ForegroundColor Yellow
        return
    }
    $name = [IO.Path]::GetFileNameWithoutExtension($exe)
    $running = @(Get-CimInstance Win32_Process -Filter "Name='$name.exe'" -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        foreach ($r in $running) {
            if (Test-SamePath $r.ExecutablePath $exe) {
                Write-Host "Mate Engine: すでに起動しています（PID $($r.ProcessId)。起動しません）"
            } else {
                Write-Host "警告: 別の場所の $name（PID $($r.ProcessId), $($r.ExecutablePath)）が起動しています。保存フォルダを奪い合うため、Mate Engine は起動しません。" -ForegroundColor Yellow
            }
        }
        return
    }
    Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe -Parent)
    Write-Host "Mate Engine: 起動しました（$exe）"
}

# --- 1. core/config.py から設定を読む（Pythonで読み込むため、コメントや計算式があっても正しい値が取れる） ---
$bootstrap = $null
$cmd = Get-Command python3.12 -ErrorAction SilentlyContinue
if ($cmd) { $bootstrap = $cmd.Source }
if (-not $bootstrap) {
    $fallback = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python3.12.exe"
    if (Test-Path $fallback) { $bootstrap = $fallback }
}
if (-not $bootstrap) { Fail "python3.12 が見つかりません。設定を読めないため中止します。" }

$code = "import json,sys; sys.path.insert(0, r'$Root'); from core import config as c; " +
        "print(json.dumps({'backend': c.LLM_BACKEND, 'api_port': c.API_PORT, 'voicevox_url': c.VOICEVOX_URL, " +
        "'voicevox_exe': c.VOICEVOX_EXE, 'ollama_url': c.OLLAMA_URL, 'main_py': c.MAIN_PY_PATH, " +
        "'main_py_python': c.MAIN_PY_PYTHON, 'log_path': c.RIN_LOG_PATH, 'key_env': c.ANTHROPIC_API_KEY_ENV, " +
        "'mate_engine_exe': c.MATE_ENGINE_EXE, 'vhd_path': c.VHD_PATH}))"
$json = & $bootstrap -X utf8 -c $code
if ($LASTEXITCODE -ne 0 -or -not $json) { Fail "core/config.py を読めませんでした。中止します。" }
$cfg = $json | ConvertFrom-Json
Write-Host "LLM_BACKEND = $($cfg.backend)"

# --- 2. Rドライブが無ければ config の VHD_PATH の VHD を自動マウントする（VHD_PATH が None なら何もしない） ---
if ($cfg.vhd_path -and -not (Test-Path "R:\")) {
    Write-Host "Rドライブが見つかりません。VHDをマウントします..."
    $disk = Mount-DiskImage -ImagePath $cfg.vhd_path -PassThru
    $volume = $disk | Get-Disk | Get-Partition | Get-Volume
    if ($volume.DriveLetter -ne 'R') {
        Set-Partition -DriveLetter $volume.DriveLetter -NewDriveLetter R
    }
    Start-Sleep -Seconds 2
}

# --- 3. anthropic のときは APIキーの環境変数を確認する（値は表示もログ出力もしない）。無ければ何も起動せず中止 ---
if ($cfg.backend -eq "anthropic") {
    $keyName = $cfg.key_env
    $keyValue = [Environment]::GetEnvironmentVariable($keyName, "Process")
    if ([string]::IsNullOrWhiteSpace($keyValue)) {
        # このウィンドウを開いた後にユーザー環境変数を設定した場合に備え、永続側も見て、あればこのプロセスに渡す
        foreach ($scope in "User", "Machine") {
            $candidate = [Environment]::GetEnvironmentVariable($keyName, $scope)
            if (-not [string]::IsNullOrWhiteSpace($candidate)) {
                $keyValue = $candidate
                [Environment]::SetEnvironmentVariable($keyName, $keyValue, "Process")
                Write-Host "$keyName を $scope 環境変数から読み込みました（値は表示しません）"
                break
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($keyValue)) {
        Write-Host "警告: 環境変数 $keyName が設定されていません。" -ForegroundColor Yellow
        Fail "LLM_BACKEND が anthropic のため、main.py は起動しません。設定してからやり直してください。"
    }
    $keyValue = $null
    Write-Host "OK: $keyName は設定されています（値は表示しません）"
}

# --- 4. 再起動モードなら Mate Engine を先に閉じ、続けて VOICEVOX（と、必要なら ollama）を止める ---
#        （Mate Engine は凛を呼ぶので、凛を止める前に閉じる。起動し直すのは最後の手順）
if ($RestartServices) {
    & "$Root\stop.ps1" -MateEngineOnly
    Write-Host "VOICEVOX を止めています..."
    Get-Process -Name "VOICEVOX" -ErrorAction SilentlyContinue | Stop-Process -Force
    if ($cfg.backend -eq "ollama") {
        Write-Host "ollama を止めています..."
        Get-Process -Name "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
    }
    Start-Sleep -Seconds 3
}

# --- 5. VOICEVOX: すでに起動していれば二重に起動しない（プロセスがある、または待ち受けポートが開いている） ---
$vvPort = ([Uri]$cfg.voicevox_url).Port
$vvRunning = [bool](Get-Process -Name "VOICEVOX" -ErrorAction SilentlyContinue) -or (Test-PortListening $vvPort)
if ($vvRunning) {
    Write-Host "VOICEVOX: すでに起動しています（起動しません）"
} elseif (Test-Path $cfg.voicevox_exe) {
    Start-Process $cfg.voicevox_exe
    Write-Host "VOICEVOX: 起動しました（読み込みに少しかかります）"
} else {
    Write-Host "警告: VOICEVOX の実行ファイルが見つかりません: $($cfg.voicevox_exe)（音声なしで続行します）" -ForegroundColor Yellow
}

# --- 6. ollama: LLM_BACKEND が ollama のときだけ起動する ---
if ($cfg.backend -eq "ollama") {
    $olPort = ([Uri]$cfg.ollama_url).Port
    if (Test-PortListening $olPort) {
        Write-Host "ollama: すでに起動しています（起動しません）"
    } else {
        Start-Process powershell -WindowStyle Hidden -ArgumentList "-Command ollama serve"
        if (Wait-Port $olPort 30 $true) {
            Write-Host "ollama: 起動しました"
        } else {
            Fail "ollama が30秒以内に起動しませんでした。中止します。"
        }
    }
} else {
    Write-Host "ollama: 起動しません（LLM_BACKEND=$($cfg.backend)）"
}

# --- 7. 既存の main.py が動いていれば止める（APIポートの待ち受けプロセスを特定し、python の main.py であることを確認してから） ---
$apiPort = [int]$cfg.api_port
$listener = Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($owner -and $owner.Name -like "python*" -and $owner.CommandLine -match "main\.py") {
        Write-Host "既存の main.py（PID $($owner.ProcessId)）を止めます..."
        # まず終了要求（'q'）を送って正常終了を待ち、止まらなければ強制終了する
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/input" -Method Post -ContentType "application/json" `
                -Body '{"text":"q"}' -TimeoutSec 3 | Out-Null
        } catch { }
        if (-not (Wait-Port $apiPort 10 $false)) {
            Write-Host "正常終了しないため、強制終了します"
            Stop-Process -Id $owner.ProcessId -Force -ErrorAction SilentlyContinue
            if (-not (Wait-Port $apiPort 5 $false)) { Fail "ポート $apiPort が解放されませんでした。中止します。" }
        }
    } else {
        $ownerName = if ($owner) { $owner.Name } else { "不明" }
        Fail "ポート $apiPort は main.py 以外のプロセス（$ownerName, PID $($listener.OwningProcess)）が使っています。止めずに中止します。"
    }
}

# --- 8. main.py を起動する（python3.12・cwd=rin\・出力は rin.log に追記。overlay が行っていた方法に合わせる） ---
$py = $cfg.main_py_python
if (-not (Test-Path $py)) { Fail "Python が見つかりません: $py" }
$mainPy = $cfg.main_py
$workDir = Split-Path $mainPy -Parent
$logPath = $cfg.log_path
$logDir = Split-Path $logPath -Parent
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -Path $logPath -Value ("`n" + ("=" * 60) + "`n[$stamp] 凛を起動`n" + ("=" * 60)) -Encoding UTF8

$cmdArgs = '/c ""' + $py + '" "' + $mainPy + '" >> "' + $logPath + '" 2>&1"'
$proc = Start-Process -FilePath "cmd.exe" -ArgumentList $cmdArgs -WorkingDirectory $workDir -WindowStyle Hidden -PassThru
Write-Host "main.py を起動しました。ポート $apiPort の待ち受けを待っています..."

# --- 9. 待ち受けになるまで待って結果を表示する ---
$timeoutSec = 90
$sw = [Diagnostics.Stopwatch]::StartNew()
while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
    if (Test-PortListening $apiPort) {
        Write-Host ("OK: main.py がポート $apiPort で待ち受けを開始しました（{0:N1}秒）" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Green
        Write-Host "ログ: $logPath"
        # --- 10. 凛の準備ができたので Mate Engine を起動する（起動直後に凛の /template と Warmup を呼ぶため、凛が先） ---
        Start-MateEngine
        exit 0
    }
    if ($proc.HasExited) {
        Write-Host "Mate Engine: 凛が起動しなかったため、起動しません。" -ForegroundColor Yellow
        Fail "main.py が起動直後に終了しました（終了コード $($proc.ExitCode)）。ログを確認してください: $logPath"
    }
    Start-Sleep -Milliseconds 500
}
Write-Host "タイムアウト: $timeoutSec 秒以内にポート $apiPort が待ち受けになりませんでした。ログを確認してください: $logPath" -ForegroundColor Yellow
Write-Host "Mate Engine: 凛の待ち受けを確認できなかったため、起動しません。" -ForegroundColor Yellow
exit 2
