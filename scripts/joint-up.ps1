# 内网联合部署（PowerShell）：ItsMyGo + rag_helper
# 用法：.\scripts\joint-up.ps1
# 两个仓库默认同级；也可先设置 $env:ITS_MYGO_DIR。
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ItsMyGoDir = if ($env:ITS_MYGO_DIR) { $env:ITS_MYGO_DIR } else { Join-Path (Split-Path -Parent $Root) "ItsMyGo" }

if (-not (Test-Path $ItsMyGoDir)) {
    Write-Host "找不到 ItsMyGo 目录：$ItsMyGoDir"
    Write-Host "请把两个仓库放成同级，或设置 ITS_MYGO_DIR。"
    exit 1
}

Set-Location $Root

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已生成 rag_helper\.env。请先填 OPENAI_API_KEY / DEEPSEEK_API_KEY / EMBEDDING_API_KEY / MINERU_API_TOKEN，再重跑本脚本。"
    exit 1
}
if (-not (Test-Path (Join-Path $ItsMyGoDir ".env"))) {
    Copy-Item (Join-Path $ItsMyGoDir ".env.example") (Join-Path $ItsMyGoDir ".env")
    Write-Host "已生成 ItsMyGo\.env（compose 有安全默认值，可直接启动）。"
}

$KeyFile = Join-Path $Root ".agent-key"
if (Test-Path $KeyFile) {
    $AgentKey = (Get-Content $KeyFile -Raw).Trim()
} else {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $AgentKey = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    Set-Content -Path $KeyFile -Value $AgentKey -NoNewline
    Write-Host "已生成共享访问密钥：$KeyFile"
}

$env:AGENT_GRPC_KEY = $AgentKey
$env:AGENT_IMPORT_KEY = $AgentKey
$env:RAG_AGENT_GRPC_TARGET = "rag-agent:8003"
$env:RAG_AGENT_IMPORT_URL = "http://rag-import:8001"

Write-Host "==> 启动 ItsMyGo（会创建共享网络 its-my-go-net）"
Push-Location $ItsMyGoDir
try { docker compose up -d --build } finally { Pop-Location }

Write-Host "==> 启动 rag_helper（接入 its-my-go-net）"
docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml up -d --build

docker compose up -d --force-recreate agent-key-init
docker compose wait agent-key-init
if ($LASTEXITCODE -ne 0) { throw "agent-key-init 执行失败，请查看 docker compose logs agent-key-init" }
docker compose logs --no-log-prefix agent-key-init | Select-Object -Last 5

Write-Host ""
Write-Host "联合部署完成："
$FrontendPort = if ($env:FRONTEND_PORT) { $env:FRONTEND_PORT } else { "8088" }
$BackendPort = if ($env:BACKEND_PORT) { $env:BACKEND_PORT } else { "8080" }
$GrpcPort = if ($env:RAG_GRPC_PORT) { $env:RAG_GRPC_PORT } else { "8003" }
$ImportPort = if ($env:RAG_IMPORT_PORT) { $env:RAG_IMPORT_PORT } else { "8001" }
Write-Host "  ItsMyGo 前端   http://localhost:$FrontendPort"
Write-Host "  ItsMyGo 后端   http://localhost:$BackendPort"
Write-Host "  rag_helper gRPC localhost:$GrpcPort（Java 通过 rag-agent:8003 访问）"
Write-Host "  知识库导入页面  http://localhost:$ImportPort/import.html"
Write-Host ""
Write-Host "共享密钥保存在 $KeyFile（已 gitignore），Java 与 agent 自动使用同一把。"
Write-Host "查看日志：docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml logs -f agent-grpc"
