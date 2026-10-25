#!/usr/bin/env bash
# rag_helper 一键部署（Docker Compose）
#
#   ./deploy.sh              启动 agent 单栈
#   ./deploy.sh joint        启动 ItsMyGo + rag_helper 联合栈
#   ./deploy.sh down         停止 agent 单栈（保留数据）
#   ./deploy.sh joint-down   停止两套（保留数据）
#   ./deploy.sh logs         跟踪 agent 日志
#   ./deploy.sh status       查看容器状态
#   ./deploy.sh reset        停止 agent 并删除其数据卷（需输入 yes）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "未安装 Docker，请先安装 Docker Desktop。"; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker Desktop 未启动，请先启动它。"; exit 1; }
}

rand_hex() {
  local bytes="${1:-32}"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  else
    od -An -tx1 -N "$bytes" /dev/urandom | tr -d ' \n'
  fi
}

env_get_file() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  grep -E "^[[:space:]]*${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '\r'
}

env_set_file() {
  local file="$1" key="$2" value="$3"
  if grep -qE "^[[:space:]]*${key}=" "$file" 2>/dev/null; then
    sed -i "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

env_get() { env_get_file .env "$1"; }

ensure_env() {
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "已生成 .env。"
  fi

  local missing=()
  local key value
  for key in OPENAI_API_KEY DEEPSEEK_API_KEY EMBEDDING_API_KEY; do
    value="$(env_get "$key")"
    if [ -z "$value" ] || [ "$value" = "sk-xxx" ]; then
      missing+=("$key")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    cat >&2 <<EOF
还差 ${#missing[@]} 个模型密钥，请编辑 $(pwd)/.env：
  OPENAI_API_KEY=...        # 百炼（qwen / 视觉）
  DEEPSEEK_API_KEY=...      # 主 agent
  EMBEDDING_API_KEY=...     # 嵌入
  MINERU_API_TOKEN=...      # 可选，只有导入 PDF 需要

填好后重跑 ./deploy.sh
EOF
    exit 1
  fi

  if [ -z "$(env_get MINERU_API_TOKEN)" ] || [ "$(env_get MINERU_API_TOKEN)" = "sk-xxx" ]; then
    echo "提示：MINERU_API_TOKEN 未填，PDF 导入不可用；Markdown 导入和问答不受影响。"
  fi

  if [ -z "$(env_get AGENT_GRPC_KEY)" ]; then
    env_set_file .env AGENT_GRPC_KEY "$(rand_hex 32)"
  fi
}

port_of() {
  local value
  value="$(env_get "$1")"
  printf '%s' "${value:-$2}"
}

wait_http() {
  local url="$1" name="$2" port
  echo "等待 ${name} 就绪..."
  for _ in $(seq 1 90); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "${name} 启动超时。查看日志：./deploy.sh logs"
  exit 1
}

wait_agent() {
  wait_http "http://127.0.0.1:$(port_of RAG_HTTP_PORT 8002)/health" "agent HTTP 服务"
}

start_agent() {
  docker compose up -d --build
  wait_agent
}

find_itsmygo() {
  local dir=""
  if [ -n "${ITS_MYGO_DIR:-}" ]; then
    dir="$ITS_MYGO_DIR"
  elif [ -f .itsmygo-dir ]; then
    dir="$(tr -d '\r\n' < .itsmygo-dir)"
  elif [ -d ../ItsMyGo ]; then
    dir="$(cd ../ItsMyGo && pwd)"
  fi

  if [ -z "$dir" ] || [ ! -f "$dir/deploy.sh" ]; then
    echo "找不到 ItsMyGo 的 deploy.sh。" >&2
    echo "请把两个仓库放成同级，或现在输入 ItsMyGo 目录：" >&2
    read -r -p "> " dir
  fi

  [ -f "$dir/deploy.sh" ] || { echo "目录里没有 deploy.sh：$dir" >&2; exit 1; }
  dir="$(cd "$dir" && pwd)"
  printf '%s\n' "$dir" > .itsmygo-dir
  printf '%s' "$dir"
}

print_summary() {
  local import_port http_port grpc_port
  import_port="$(port_of RAG_IMPORT_PORT 8001)"
  http_port="$(port_of RAG_HTTP_PORT 8002)"
  grpc_port="$(port_of RAG_GRPC_PORT 8003)"
  echo
  echo "rag_helper 已启动："
  echo "  导入页面  http://localhost:${import_port}/import.html"
  echo "  聊天页面  http://localhost:${http_port}/chat.html"
  echo "  gRPC      localhost:${grpc_port}"
  echo "停止：./deploy.sh down    日志：./deploy.sh logs"
}

cmd="${1:-up}"
shift || true

case "$cmd" in
  up)
    need_docker
    ensure_env
    start_agent
    print_summary
    ;;
  joint)
    need_docker
    ensure_env
    ITS_MYGO="$(find_itsmygo)"
    SHARED_KEY="$(env_get AGENT_GRPC_KEY)"
    [ -f "$ITS_MYGO/.env" ] || cp "$ITS_MYGO/.env.example" "$ITS_MYGO/.env"
    env_set_file "$ITS_MYGO/.env" AGENT_GRPC_KEY "$SHARED_KEY"
    env_set_file "$ITS_MYGO/.env" AGENT_IMPORT_KEY "$SHARED_KEY"
    export RAG_AGENT_GRPC_TARGET=rag-agent:8003
    export RAG_AGENT_IMPORT_URL=http://rag-import:8001

    echo "==> 启动 ItsMyGo"
    bash "$ITS_MYGO/deploy.sh" up

    echo "==> 启动 rag_helper（接入 its-my-go-net）"
    docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml up -d --build
    wait_agent
    docker compose up -d --force-recreate agent-key-init
    docker compose wait agent-key-init

    frontend_port="$(env_get_file "$ITS_MYGO/.env" FRONTEND_PORT)"
    echo
    echo "联合部署已启动："
    echo "  ItsMyGo   http://localhost:${frontend_port:-8088}"
    echo "  gRPC      rag-agent:8003（Java 侧）"
    echo "  导入      rag-import:8001（Java 侧）"
    echo "停止两套：./deploy.sh joint-down"
    ;;
  down)
    need_docker
    docker compose down
    echo "已停止（数据保留）。"
    ;;
  joint-down)
    need_docker
    docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml down
    ITS_MYGO="${ITS_MYGO_DIR:-}"
    if [ -z "$ITS_MYGO" ] && [ -f .itsmygo-dir ]; then
      ITS_MYGO="$(tr -d '\r\n' < .itsmygo-dir)"
    fi
    if [ -n "$ITS_MYGO" ] && [ -f "$ITS_MYGO/deploy.sh" ]; then
      bash "$ITS_MYGO/deploy.sh" down
    fi
    echo "已停止（数据保留）。"
    ;;
  logs)
    docker compose logs -f agent-grpc
    ;;
  status)
    docker compose ps
    ;;
  reset)
    need_docker
    read -r -p "会删除 rag_helper 的数据卷，输入 yes 继续：" answer
    [ "$answer" = "yes" ] || { echo "已取消。"; exit 1; }
    docker compose down -v
    echo "已删除容器与数据卷。"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "未知命令：$cmd"
    usage
    exit 1
    ;;
esac
