#!/usr/bin/env bash
# rag_helper 单栈一键启动（Docker Compose）
# 用法：./scripts/docker-up.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 .env。请先填 OPENAI_API_KEY / DEEPSEEK_API_KEY / EMBEDDING_API_KEY / MINERU_API_TOKEN，再重跑本脚本。"
  exit 1
fi

if grep -Eq '^(OPENAI_API_KEY|DEEPSEEK_API_KEY|EMBEDDING_API_KEY)=sk-xxx$' .env; then
  echo "警告：.env 里仍有 sk-xxx 占位符，agent / 嵌入调用会失败。"
fi

docker compose up -d --build

echo
echo "服务地址："
echo "  知识库导入  http://localhost:${RAG_IMPORT_PORT:-8001}/import.html"
echo "  聊天页面    http://localhost:${RAG_HTTP_PORT:-8002}/chat.html"
echo "  gRPC        localhost:${RAG_GRPC_PORT:-8003}（Java 侧目标）"
echo "  MinIO 控制台 http://localhost:${RAG_MINIO_CONSOLE_PORT:-9003}"
echo
echo "Java 访问密钥（.env 的 AGENT_GRPC_KEY）："
if [ -n "${AGENT_GRPC_KEY:-}" ]; then
  echo "  已从环境变量注入；agent-key-init 会把它写入 MongoDB。"
else
  echo "  未设置。首次启动的 agent-key-init 日志里有一次性的明文密钥："
  echo "  docker compose logs agent-key-init"
fi
echo
echo "查看状态：docker compose ps"
echo "查看日志：docker compose logs -f agent-grpc"
