# rag_helper 单栈一键启动（PowerShell）
# 用法：.\scripts\docker-up.ps1
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "已生成 .env。请先填 OPENAI_API_KEY / DEEPSEEK_API_KEY / EMBEDDING_API_KEY / MINERU_API_TOKEN，再重跑本脚本。"
    exit 1
}

if (Select-String -Path ".env" -Pattern "^(OPENAI_API_KEY|DEEPSEEK_API_KEY|EMBEDDING_API_KEY)=sk-xxx$" -Quiet) {
    Write-Host "警告：.env 里仍有 sk-xxx 占位符，agent / 嵌入调用会失败。"
}

docker compose up -d --build

$ImportPort = if ($env:RAG_IMPORT_PORT) { $env:RAG_IMPORT_PORT } else { "8001" }
$HttpPort = if ($env:RAG_HTTP_PORT) { $env:RAG_HTTP_PORT } else { "8002" }
$GrpcPort = if ($env:RAG_GRPC_PORT) { $env:RAG_GRPC_PORT } else { "8003" }
$MinioConsolePort = if ($env:RAG_MINIO_CONSOLE_PORT) { $env:RAG_MINIO_CONSOLE_PORT } else { "9003" }

Write-Host ""
Write-Host "服务地址："
Write-Host "  知识库导入  http://localhost:$ImportPort/import.html"
Write-Host "  聊天页面    http://localhost:$HttpPort/chat.html"
Write-Host "  gRPC        localhost:$GrpcPort（Java 侧目标）"
Write-Host "  MinIO 控制台 http://localhost:$MinioConsolePort"
Write-Host ""
Write-Host "Java 访问密钥：未设置 AGENT_GRPC_KEY 时，首次启动的 agent-key-init 日志里有一次性的明文密钥："
Write-Host "  docker compose logs agent-key-init"
Write-Host ""
Write-Host "查看状态：docker compose ps"
