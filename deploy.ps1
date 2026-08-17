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

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONPATH = "src"

python -m jpgate scan
if (-not $NoNotify) { python -m jpgate notify --no-dry-run }
python -m jpgate publish

# 内容が変わっていないときに空コミットを積まない。
git add docs
if (git diff --cached --quiet) {
    Write-Host "ページに変更なし。プッシュしません。"
    exit 0
}

$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "sites: $stamp 時点の一覧に更新"
git push origin main
Write-Host "反映しました: https://tknjnsk.github.io/jpgate/"
