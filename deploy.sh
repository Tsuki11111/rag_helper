#!/usr/bin/env bash
# rag_helper 部署入口（轻量转发）
#
# **联合启动/停止入口已收归 ItsMyGo 仓库**：
#   演示环境联合栈 → ItsMyGo/deploy.sh joint / joint-down
#   本脚本只做转发，保留 rag_helper 侧的日志/状态/重置命令。
#
#   ./deploy.sh              启动 ItsMyGo + rag_helper 联合栈（等同 joint，转发）
#   ./deploy.sh joint        同 up
#   ./deploy.sh down         停止两套（转发 ItsMyGo/deploy.sh joint-down）
#   ./deploy.sh joint-down   同 down
#   ./deploy.sh logs         跟踪 agent 日志
#   ./deploy.sh status       查看 agent 容器状态
#   ./deploy.sh reset        停止 agent 并删除其数据卷（需输入 yes）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "未安装 Docker，请先安装 Docker Desktop。"; exit 1; }
  docker info >/dev/null 2>&1 || { echo "Docker Desktop 未启动，请先启动它。"; exit 1; }
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
    echo "请把两个仓库放成同级，或设置 ITS_MYGO_DIR，或在 rag_helper 根写 .itsmygo-dir。" >&2
    exit 1
  fi
  dir="$(cd "$dir" && pwd)"
  printf '%s' "$dir" > .itsmygo-dir
  printf '%s' "$dir"
}

cmd="${1:-joint}"
shift || true

case "$cmd" in
  up|joint)
    ITS_MYGO="$(find_itsmygo)"
    echo "联合启动入口已收归 ItsMyGo，转发到 $ITS_MYGO/deploy.sh joint"
    exec bash "$ITS_MYGO/deploy.sh" joint "$@"
    ;;
  down|joint-down)
    ITS_MYGO="$(find_itsmygo)"
    echo "联合停止入口已收归 ItsMyGo，转发到 $ITS_MYGO/deploy.sh joint-down"
    exec bash "$ITS_MYGO/deploy.sh" joint-down "$@"
    ;;
  logs)
    need_docker
    docker compose logs -f agent-grpc
    ;;
  status)
    need_docker
    docker compose ps
    ;;
  reset)
    need_docker
    read -r -p "会删除 rag_helper 的数据卷，输入 yes 继续： " answer
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
