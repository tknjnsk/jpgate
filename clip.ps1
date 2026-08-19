# 1日1回、TikTok 用の縦動画を作って Discord へ置く。
#
# **deploy.ps1 とは分けてある。** 混ぜない理由は2つ:
#   - deploy.ps1 は毎時走り、失敗しても止まらない設計（走査が落ちた回は
#     publish が前回と同じページを作るだけ）。動画は ffmpeg と Chromium を
#     起こすので失敗の質が違う。混ぜると毎時のサイト更新が動画の都合で落ちうる。
#   - 動画は在庫を消費する（1回5件）。毎時の経路と時刻を分けておきたい。
#
# 使い方:  powershell -ExecutionPolicy Bypass -File clip.ps1
#          -DryRun を付けると動画は作るが Discord へ送らない
#
# タスクスケジューラ "JPGate Clip" が毎日20:00に叩く。PCがスリープでも
# 起きて実行する（WakeToRun）。**完全にシャットダウンしていると起きない** —
# これはWindowsの制約で、スケジューラ側では回避できない。

param([switch]$DryRun)

# "Stop" にしない理由は deploy.ps1 と同じ。Windows PowerShell 5.1 は
# ネイティブexeのstderrを ErrorRecord に包むため、正常終了でも失敗に見える。
# 成否は $? ではなく $LASTEXITCODE で見る。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONPATH = "src"

# PATH と Webhook を**レジストリから取り直す**。どちらもプロセス起動時の
# コピーを引き継ぐので、古い環境から起動されると見つからない。ffmpeg は
# winget が User の PATH に足すため、インストール前に開いていたシェルから
# 叩くと「ffmpeg が見つかりません」で落ちる（実際に踏んだ）。
# タスクスケジューラからは正しく入るが、手で叩く経路でも同じように動くべき。
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
            [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $env:JPGATE_CLIP_WEBHOOK) {
    $env:JPGATE_CLIP_WEBHOOK = [Environment]::GetEnvironmentVariable('JPGATE_CLIP_WEBHOOK', 'User')
}

$webhook = $env:JPGATE_CLIP_WEBHOOK

function Send-Failure([string]$text) {
    # **失敗を成功と同じ場所に出す。** 20:00に無人で走るので、黙って落ちると
    # 「今日は新着が無かった」のか「壊れている」のか区別がつかない。
    # 成功したら動画が届くチャンネルに、失敗したらその旨が届くようにする。
    if (-not $webhook) { return }
    $body = @{ content = $text } | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Uri $webhook -Method Post -ContentType 'application/json; charset=utf-8' `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
    } catch {
        Write-Warning "失敗の通知も送れませんでした: $_"
    }
}

if (-not $DryRun -and -not $webhook) {
    Write-Error "JPGATE_CLIP_WEBHOOK が未設定です。送信できないので実行しません。"
    exit 1
}

# --- 生成と送信 -------------------------------------------------------------
$args = @("-m", "jpgate", "clip")
if (-not $DryRun) { $args += "--no-dry-run" }

$log = Join-Path $PSScriptRoot "data\clips\last-run.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

$out = & python $args 2>&1 | Out-String
$code = $LASTEXITCODE
"[{0}] exit={1}`n{2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $code, $out |
    Out-File -Encoding utf8 $log

Write-Host $out
if ($code -ne 0) {
    # 末尾だけ載せる。Discord の content は2000字上限で、超えると通知そのものが
    # 落ちる（失敗を知らせる経路が失敗するのが最悪）。
    $tail = $out.Trim()
    if ($tail.Length -gt 1500) { $tail = $tail.Substring($tail.Length - 1500) }
    Send-Failure ("**JPGate: 動画の生成に失敗しました** (exit $code)`n``````" + "`n$tail`n" + "``````")
    exit 1
}

# --- 古い動画を片付ける -----------------------------------------------------
# TikTok へ上げたあとの mp4 に用は無い（Discord にも残っている）。放っておくと
# 1日6MB前後ずつ増え続けるので、直近14日ぶんだけ残す。
Get-ChildItem (Join-Path $PSScriptRoot "data\clips") -Filter *.mp4 -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 14 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit 0
