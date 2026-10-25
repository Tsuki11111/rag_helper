# 停止联合部署（PowerShell，保留所有数据卷）
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ItsMyGoDir = if ($env:ITS_MYGO_DIR) { $env:ITS_MYGO_DIR } else { Join-Path (Split-Path -Parent $Root) "ItsMyGo" }

Set-Location $Root
docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml down
if (Test-Path $ItsMyGoDir) {
    Push-Location $ItsMyGoDir
    try { docker compose down } finally { Pop-Location }
}
Write-Host "已停止。数据卷保留；要连数据一起清掉，在各仓库执行 docker compose down -v。"
