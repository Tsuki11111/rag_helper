#!/usr/bin/env bash
# 内网联合部署：ItsMyGo + rag_helper
#
# 前置：两个仓库是同级目录，或设置 ITS_MYGO_DIR 指向 ItsMyGo。
#   parent/
#     ItsMyGo/
#     rag_helper/
#
# 用法：
#   ./scripts/joint-up.sh
#   ITS_MYGO_DIR=/path/to/ItsMyGo ./scripts/joint-up.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITS_MYGO_DIR="${ITS_MYGO_DIR:-$(cd "$ROOT/.." && pwd)/ItsMyGo}"

if [ ! -d "$ITS_MYGO_DIR" ]; then
  echo "找不到 ItsMyGo 目录：$ITS_MYGO_DIR"
  echo "请把两个仓库放成同级，或设置 ITS_MYGO_DIR。"
  exit 1
fi

cd "$ROOT"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "已生成 rag_helper/.env。请先填 OPENAI_API_KEY / DEEPSEEK_API_KEY / EMBEDDING_API_KEY / MINERU_API_TOKEN，再重跑本脚本。"
  exit 1
fi
if [ ! -f "$ITS_MYGO_DIR/.env" ]; then
  cp "$ITS_MYGO_DIR/.env.example" "$ITS_MYGO_DIR/.env"
  echo "已生成 ItsMyGo/.env（compose 有安全默认值，可直接启动）。"
fi

# Java ↔ agent 共享密钥：生成一次后复用，绝不入库
KEY_FILE="$ROOT/.agent-key"
if [ -s "$KEY_FILE" ]; then
  AGENT_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
else
  if command -v openssl >/dev/null 2>&1; then
    AGENT_KEY="$(openssl rand -hex 32)"
  else
    AGENT_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  fi
  umask 077
  printf '%s\n' "$AGENT_KEY" > "$KEY_FILE"
  echo "已生成共享访问密钥：$KEY_FILE"
fi

# 两个 compose 必须看到同一把密钥；shell 环境变量优先级高于 .env
export AGENT_GRPC_KEY="$AGENT_KEY"
export AGENT_IMPORT_KEY="$AGENT_KEY"
# Docker 内用服务别名，不要让 .env 里本地调试的 127.0.0.1 生效
export RAG_AGENT_GRPC_TARGET=rag-agent:8003
export RAG_AGENT_IMPORT_URL=http://rag-import:8001

echo "==> 启动 ItsMyGo（会创建共享网络 its-my-go-net）"
(cd "$ITS_MYGO_DIR" && docker compose up -d --build)

echo "==> 启动 rag_helper（接入 its-my-go-net）"
docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml up -d --build

# 把密钥写入 MongoDB（幂等；重复执行只会把密钥对齐成当前值）
docker compose up -d --force-recreate agent-key-init
docker compose wait agent-key-init
docker compose logs --no-log-prefix agent-key-init | tail -5

echo
echo "联合部署完成："
echo "  ItsMyGo 前端   http://localhost:${FRONTEND_PORT:-8088}"
echo "  ItsMyGo 后端   http://localhost:${BACKEND_PORT:-8080}"
echo "  rag_helper gRPC localhost:${RAG_GRPC_PORT:-8003}（Java 通过 rag-agent:8003 访问）"
echo "  知识库导入页面  http://localhost:${RAG_IMPORT_PORT:-8001}/import.html"
echo
echo "Java 侧已注入：AGENT_GRPC_TARGET=rag-agent:8003 / AGENT_IMPORT_URL=http://rag-import:8001"
echo "共享密钥保存在 $KEY_FILE（已 gitignore），Java 与 agent 自动使用同一把。"
echo
if ! grep -q '^BOOTSTRAP_ADMIN_USERNAME=.\+' "$ITS_MYGO_DIR/.env"; then
  echo "提示：ItsMyGo/.env 未配置 BOOTSTRAP_ADMIN_USERNAME/PASSWORD，首次启动不会自动创建管理员。"
  echo "     配置后重启 backend：cd \"$ITS_MYGO_DIR\" && docker compose up -d backend"
fi
echo "查看日志：docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml logs -f agent-grpc"
