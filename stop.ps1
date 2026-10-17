# 凛OS 停止スクリプト: Mate Engine → 凛（main.py）の順に止める。VOICEVOX は -StopVoicevox を付けたときだけ止める。
#   使い方: powershell -ExecutionPolicy Bypass -File stop.ps1 [-StopVoicevox]
#   -MateEngineOnly: Mate Engine だけ止める（run.ps1 -RestartServices から使う）
# 止めるのは、core/config.py の MATE_ENGINE_EXE と同じ実行ファイルの MateEngineX だけ（Steam版などは止めず、警告する）。
param([switch]$StopVoicevox, [switch]$MateEngineOnly)

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

# --- 設定を core/config.py から読む（run.ps1 と同じ方法） ---
$bootstrap = $null
$cmd = Get-Command python3.12 -ErrorAction SilentlyContinue
if ($cmd) { $bootstrap = $cmd.Source }
if (-not $bootstrap) {
    $fallback = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\python3.12.exe"
    if (Test-Path $fallback) { $bootstrap = $fallback }
}
if (-not $bootstrap) { Fail "python3.12 が見つかりません。設定を読めないため中止します。" }

$code = "import json,sys; sys.path.insert(0, r'$Root'); from core import config as c; " +
        "print(json.dumps({'api_port': c.API_PORT, 'mate_engine_exe': c.MATE_ENGINE_EXE}))"
$json = & $bootstrap -X utf8 -c $code
if ($LASTEXITCODE -ne 0 -or -not $json) { Fail "core/config.py を読めませんでした。中止します。" }
$cfg = $json | ConvertFrom-Json

# --- 1. Mate Engine を閉じる（CloseMainWindow → 5秒待つ → 残っていれば強制終了） ---
$meName = [IO.Path]::GetFileNameWithoutExtension($cfg.mate_engine_exe)
$meProcs = @(Get-CimInstance Win32_Process -Filter "Name='$meName.exe'" -ErrorAction SilentlyContinue)
$mine = @($meProcs | Where-Object { Test-SamePath $_.ExecutablePath $cfg.mate_engine_exe })
$others = @($meProcs | Where-Object { -not (Test-SamePath $_.ExecutablePath $cfg.mate_engine_exe) })
foreach ($o in $others) {
    Write-Host "警告: 別の場所の $meName（PID $($o.ProcessId), $($o.ExecutablePath)）は止めません。" -ForegroundColor Yellow
}
if ($mine.Count -eq 0) {
    Write-Host "Mate Engine: 起動していません"
}
foreach ($m in $mine) {
    $p = Get-Process -Id $m.ProcessId -ErrorAction SilentlyContinue
    if (-not $p) { continue }
    Write-Host "Mate Engine（PID $($m.ProcessId)）を閉じます..."
    [void]$p.CloseMainWindow()
    if ($p.WaitForExit(5000)) {
        Write-Host "Mate Engine: 正常に閉じました" -ForegroundColor Green
    } else {
        Write-Host "5秒以内に閉じないため、強制終了します"
        Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue
        if ($p.WaitForExit(5000)) {
            Write-Host "Mate Engine: 強制終了しました" -ForegroundColor Yellow
        } else {
            Write-Host "警告: Mate Engine（PID $($m.ProcessId)）が終了しませんでした。" -ForegroundColor Yellow
        }
    }
}
if ($MateEngineOnly) { exit 0 }

# --- 2. 凛（main.py）を止める（APIポートの待ち受けプロセスが python の main.py であることを確認してから） ---
$apiPort = [int]$cfg.api_port
$listener = Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction SilentlyContinue | Select-Object -First 1
$exitCode = 0
if (-not $listener) {
    Write-Host "凛: 起動していません"
} else {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($owner -and $owner.Name -like "python*" -and $owner.CommandLine -match "main\.py") {
        Write-Host "凛（main.py, PID $($owner.ProcessId)）を止めます..."
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/input" -Method Post -ContentType "application/json" `
                -Body '{"text":"q"}' -TimeoutSec 3 | Out-Null
        } catch { }
        if (Wait-Port $apiPort 10 $false) {
            Write-Host "凛: 正常に止まりました" -ForegroundColor Green
        } else {
            Write-Host "10秒以内に止まらないため、強制終了します"
            Stop-Process -Id $owner.ProcessId -Force -ErrorAction SilentlyContinue
            if (Wait-Port $apiPort 5 $false) {
                Write-Host "凛: 強制終了しました" -ForegroundColor Yellow
            } else {
                Write-Host "警告: ポート $apiPort が解放されませんでした。" -ForegroundColor Yellow
                $exitCode = 1
            }
        }
    } else {
        $ownerName = if ($owner) { $owner.Name } else { "不明" }
        Write-Host "警告: ポート $apiPort は main.py 以外のプロセス（$ownerName, PID $($listener.OwningProcess)）が使っています。止めません。" -ForegroundColor Yellow
        $exitCode = 1
    }
}

# --- 3. VOICEVOX（-StopVoicevox のときだけ） ---
if ($StopVoicevox) {
    $vv = @(Get-Process -Name "VOICEVOX" -ErrorAction SilentlyContinue)
    if ($vv.Count -eq 0) {
        Write-Host "VOICEVOX: 起動していません"
    } else {
        $vv | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Host "VOICEVOX: 止めました（$($vv.Count)プロセス）"
    }
} else {
    Write-Host "VOICEVOX: 止めません（止めるときは -StopVoicevox を付けてください）"
}
exit $exitCode
