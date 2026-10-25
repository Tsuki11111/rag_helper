param(
    [Parameter(Position = 0)]
    [string]$Command = "up"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSCommandPath
Set-Location $Root
$EnvFile = Join-Path $Root ".env"

function Show-Usage {
    Write-Host "./deploy.ps1              启动 agent 单栈"
    Write-Host "./deploy.ps1 joint        启动 ItsMyGo + rag_helper 联合栈"
    Write-Host "./deploy.ps1 down         停止 agent 单栈（保留数据）"
    Write-Host "./deploy.ps1 joint-down   停止两套（保留数据）"
    Write-Host "./deploy.ps1 logs         跟踪 agent 日志"
    Write-Host "./deploy.ps1 status       查看容器状态"
    Write-Host "./deploy.ps1 reset        停止 agent 并删除其数据卷（需输入 yes）"
}

function Test-Docker {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop 未启动，请先启动它。"
    }
}

function New-Hex([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($buffer)
    $rng.Dispose()
    ($buffer | ForEach-Object { $_.ToString("x2") }) -join ""
}

function Get-EnvValueFile([string]$File, [string]$Key) {
    if (-not (Test-Path $File)) { return "" }
    $line = Get-Content $File -Encoding UTF8 |
        Where-Object { $_ -match "^$Key=" } |
        Select-Object -Last 1
    if (-not $line) { return "" }
    return (($line -split "=", 2)[1]).Trim()
}

function Set-EnvValueFile([string]$File, [string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path $File) { $lines = @(Get-Content $File -Encoding UTF8) }
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$Key=") {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines += "$Key=$Value" }
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($File, $lines, $utf8)
}

function Get-EnvValue([string]$Key) {
    return Get-EnvValueFile $EnvFile $Key
}

function Ensure-Env {
    if (-not (Test-Path $EnvFile)) {
        Copy-Item ".env.example" $EnvFile
        Write-Host "已生成 .env。"
    }

    $missing = @()
    foreach ($key in @("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "EMBEDDING_API_KEY")) {
        $value = Get-EnvValue $key
        if (-not $value -or $value -eq "sk-xxx") { $missing += $key }
    }
    if ($missing.Count -gt 0) {
        Write-Host "还差 $($missing.Count) 个模型密钥，请编辑 $EnvFile：" -ForegroundColor Yellow
        Write-Host "  OPENAI_API_KEY=...        # 百炼（qwen / 视觉）"
        Write-Host "  DEEPSEEK_API_KEY=...      # 主 agent"
        Write-Host "  EMBEDDING_API_KEY=...     # 嵌入"
        Write-Host "  MINERU_API_TOKEN=...      # 可选，只有导入 PDF 需要"
        Write-Host ""
        Write-Host "填好后重跑 ./deploy.ps1"
        exit 1
    }

    $mineru = Get-EnvValue "MINERU_API_TOKEN"
    if (-not $mineru -or $mineru -eq "sk-xxx") {
        Write-Host "提示：MINERU_API_TOKEN 未填，PDF 导入不可用；Markdown 导入和问答不受影响。"
    }

    if (-not (Get-EnvValue "AGENT_GRPC_KEY")) {
        Set-EnvValueFile $EnvFile "AGENT_GRPC_KEY" (New-Hex 32)
    }
}

function Get-Port([string]$Key, [string]$Default) {
    $value = Get-EnvValue $Key
    if (-not $value) { $value = $Default }
    return $value
}

function Wait-Http([string]$Url, [string]$Name) {
    Write-Host "等待 $Name 就绪..."
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return }
        } catch {
            # 还没起来，继续等
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name 启动超时。查看日志：./deploy.ps1 logs"
}

function Wait-Agent {
    $port = Get-Port "RAG_HTTP_PORT" "8002"
    Wait-Http "http://127.0.0.1:$port/health" "agent HTTP 服务"
}

function Find-ItsMyGo {
    $dir = $env:ITS_MYGO_DIR
    if (-not $dir -and (Test-Path ".itsmygo-dir")) {
        $dir = (Get-Content ".itsmygo-dir" -Raw).Trim()
    }
    if (-not $dir) {
        $sibling = Join-Path (Split-Path -Parent $Root) "ItsMyGo"
        if (Test-Path $sibling) { $dir = $sibling }
    }
    while (-not $dir -or -not (Test-Path (Join-Path $dir "deploy.ps1"))) {
        $dir = Read-Host "ItsMyGo 目录（含 deploy.ps1）"
    }
    $dir = (Resolve-Path $dir).Path
    Set-Content -Path ".itsmygo-dir" -Value $dir -NoNewline
    return $dir
}

function Show-Summary {
    $importPort = Get-Port "RAG_IMPORT_PORT" "8001"
    $httpPort = Get-Port "RAG_HTTP_PORT" "8002"
    $grpcPort = Get-Port "RAG_GRPC_PORT" "8003"
    Write-Host ""
    Write-Host "rag_helper 已启动："
    Write-Host "  导入页面  http://localhost:$importPort/import.html"
    Write-Host "  聊天页面  http://localhost:$httpPort/chat.html"
    Write-Host "  gRPC      localhost:$grpcPort"
    Write-Host "停止：./deploy.ps1 down    日志：./deploy.ps1 logs"
}

switch ($Command.ToLower()) {
    "up" {
        Test-Docker
        Ensure-Env
        docker compose up -d --build
        Wait-Agent
        Show-Summary
    }
    "joint" {
        Test-Docker
        Ensure-Env
        $its = Find-ItsMyGo
        $sharedKey = Get-EnvValue "AGENT_GRPC_KEY"
        $itsEnv = Join-Path $its ".env"
        if (-not (Test-Path $itsEnv)) {
            Copy-Item (Join-Path $its ".env.example") $itsEnv
        }
        Set-EnvValueFile $itsEnv "AGENT_GRPC_KEY" $sharedKey
        Set-EnvValueFile $itsEnv "AGENT_IMPORT_KEY" $sharedKey
        $env:RAG_AGENT_GRPC_TARGET = "rag-agent:8003"
        $env:RAG_AGENT_IMPORT_URL = "http://rag-import:8001"

        Write-Host "==> 启动 ItsMyGo"
        & (Join-Path $its "deploy.ps1") up

        Write-Host "==> 启动 rag_helper（接入 its-my-go-net）"
        docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml up -d --build
        Wait-Agent
        docker compose up -d --force-recreate agent-key-init
        docker compose wait agent-key-init

        $frontendPort = Get-EnvValueFile $itsEnv "FRONTEND_PORT"
        if (-not $frontendPort) { $frontendPort = "8088" }
        Write-Host ""
        Write-Host "联合部署已启动："
        Write-Host "  ItsMyGo   http://localhost:$frontendPort"
        Write-Host "  gRPC      rag-agent:8003（Java 侧）"
        Write-Host "  导入      rag-import:8001（Java 侧）"
        Write-Host "停止两套：./deploy.ps1 joint-down"
    }
    "down" {
        Test-Docker
        docker compose down
        Write-Host "已停止（数据保留）。"
    }
    "joint-down" {
        Test-Docker
        docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml down
        $its = $env:ITS_MYGO_DIR
        if (-not $its -and (Test-Path ".itsmygo-dir")) {
            $its = (Get-Content ".itsmygo-dir" -Raw).Trim()
        }
        if ($its -and (Test-Path (Join-Path $its "deploy.ps1"))) {
            & (Join-Path $its "deploy.ps1") down
        }
        Write-Host "已停止（数据保留）。"
    }
    "logs" { docker compose logs -f agent-grpc }
    "status" { docker compose ps }
    "reset" {
        Test-Docker
        $answer = Read-Host "会删除 rag_helper 的数据卷，输入 yes 继续"
        if ($answer -ne "yes") { Write-Host "已取消。"; exit 1 }
        docker compose down -v
        Write-Host "已删除容器与数据卷。"
    }
    default { Show-Usage }
}
