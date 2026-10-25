#!/usr/bin/env bash
# 停止联合部署（保留所有数据卷）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ITS_MYGO_DIR="${ITS_MYGO_DIR:-$(cd "$ROOT/.." && pwd)/ItsMyGo}"

cd "$ROOT"
docker compose -f docker-compose.yml -f docker-compose.itsmygo.yml down
if [ -d "$ITS_MYGO_DIR" ]; then
  (cd "$ITS_MYGO_DIR" && docker compose down)
fi
echo "已停止。数据卷保留；要连数据一起清掉，在各仓库执行 docker compose down -v。"
