# 走査 → 通知 → ページ生成 → GitHub Pages へ反映。
#
# **走査は必ずこのPCから実行すること。** GitHub Actions のランナーはデータセンターの
# IPで、プレミアムバンダイは非ブラウザからのアクセスをチャレンジで弾く（実測済み。
# 素のUAではトップページすら通らない）。クラウドで走らせると静かに0件になる。
# Actions に任せてよいのは「生成済みの docs/ を配信すること」だけ。
#
# 使い方:  powershell -ExecutionPolicy Bypass -File deploy.ps1
#          -NoNotify を付けると Discord へ送らない（ページだけ更新）

param([switch]$NoNotify)

# "Stop" にしてはいけない。git は進捗や push 結果を**正常時でも stderr に書く**が、
# Windows PowerShell 5.1 はネイティブexeのstderrを ErrorRecord に包むため、
# 成功した push が終了コード1で落ちる（実際に踏んだ）。
# 代わりに $LASTEXITCODE を各所で明示的に見る。
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$env:PYTHONPATH = "src"

# --- 走査 -------------------------------------------------------------------
# 失敗しても中断しない。走査が落ちた回は DB に反映されないので、publish は
# 前回と同じページを作り、差分が出ずにプッシュされない。放置しても壊れない。
python -m jpgate scan
if ($LASTEXITCODE -ne 0) {
    Write-Warning "走査に失敗したショップがあります。反映されていない可能性があります。"
}

# --- 通知 -------------------------------------------------------------------
# Webhook 未設定のまま notify を呼ぶと毎回エラーで終わる。まだ Discord を
# 作っていない段階でも回せるように、ここで明示的に飛ばす。
if ($NoNotify) {
    Write-Host "通知はスキップします (-NoNotify)。"
} elseif (-not $env:JPGATE_DISCORD_WEBHOOK) {
    Write-Host "JPGATE_DISCORD_WEBHOOK が未設定のため通知しません。"
    Write-Host "  未通知イベントは DB に残るので、設定後に notify すれば流せます。"
} else {
    python -m jpgate notify --no-dry-run
    if ($LASTEXITCODE -ne 0) { Write-Warning "通知に失敗しました。" }
}

# --- ページ生成 -------------------------------------------------------------
# 掲載0件のときは publish 自身が書き換えを拒否して 1 を返す。
# その状態で git add に進むと「空のサイト」を公開しかねないので、ここで止める。
python -m jpgate publish
if ($LASTEXITCODE -ne 0) {
    Write-Error "ページを生成できませんでした。プッシュを中止します。"
    exit 1
}

# --- 反映 -------------------------------------------------------------------
git add docs
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "ページに変更なし。プッシュしません。"
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "site: $stamp 時点の一覧に更新"
if ($LASTEXITCODE -ne 0) { Write-Error "コミットに失敗しました。"; exit 1 }

git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "プッシュに失敗しました。"; exit 1 }

Write-Host "反映しました: https://jpgate.net/"
