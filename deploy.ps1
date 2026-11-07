param(
    [Parameter(Position = 0)]
    [string]$Command = "joint"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
Set-Location $Root

function Show-Usage {
    Write-Host "rag_helper 部署入口（轻量转发）"
    Write-Host ""
    Write-Host "联合启动/停止入口已收归 ItsMyGo 仓库；本脚本转发给 ItsMyGo/deploy.ps1。"
    Write-Host ""
    Write-Host "./deploy.ps1              启动 ItsMyGo + rag_helper 联合栈（等同 joint，转发）"
    Write-Host "./deploy.ps1 joint        同 up"
    Write-Host "./deploy.ps1 down         停止两套（转发 joint-down）"
    Write-Host "./deploy.ps1 joint-down   同 down"
    Write-Host "./deploy.ps1 logs         跟踪 agent 日志"
    Write-Host "./deploy.ps1 status       查看 agent 容器状态"
    Write-Host "./deploy.ps1 reset        停止 agent 并删除其数据卷（需输入 yes）"
}

function Test-Docker {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop 未启动，请先启动它。"
    }
}

function Find-ItsMyGo {
    $dir = $env:ITS_MYGO_DIR
    if (-not $dir -and (Test-Path ".itsmygo-dir")) {
        $dir = (Get-Content ".itsmygo-dir" -Raw).Trim()
    }
    if (-not $dir) {
        $sibling = Join-Path (Split-Path -Parent $Root) "ItsMyGo"
        if (Test-Path (Join-Path $sibling "deploy.ps1")) { $dir = $sibling }
    }
    if (-not $dir -or -not (Test-Path (Join-Path $dir "deploy.ps1"))) {
        throw "找不到 ItsMyGo 的 deploy.ps1。请设置 ITS_MYGO_DIR，或在 rag_helper 根写 .itsmygo-dir。"
    }
    $dir = (Resolve-Path $dir).Path
    Set-Content -Path ".itsmygo-dir" -Value $dir -NoNewline
    return $dir
}

if ($Command.ToLower() -eq "up") { $Command = "joint" }

switch ($Command.ToLower()) {
    "joint" {
        $its = Find-ItsMyGo
        Write-Host "联合启动入口已收归 ItsMyGo，转发到 $its\deploy.ps1 joint"
        & (Join-Path $its "deploy.ps1") joint
        exit $LASTEXITCODE
    }
    "down" {
        $its = Find-ItsMyGo
        Write-Host "联合停止入口已收归 ItsMyGo，转发到 $its\deploy.ps1 joint-down"
        & (Join-Path $its "deploy.ps1") joint-down
        exit $LASTEXITCODE
    }
    "joint-down" {
        $its = Find-ItsMyGo
        & (Join-Path $its "deploy.ps1") joint-down
        exit $LASTEXITCODE
    }
    "logs" { Test-Docker; docker compose logs -f agent-grpc }
    "status" { Test-Docker; docker compose ps }
    "reset" {
        Test-Docker
        $answer = Read-Host "会删除 rag_helper 的数据卷，输入 yes 继续"
        if ($answer -ne "yes") { Write-Host "已取消。"; exit 1 }
        docker compose down -v
        Write-Host "已删除容器与数据卷。"
    }
    default { Show-Usage }
}
