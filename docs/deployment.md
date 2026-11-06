# 部署说明 · rag_helper

> **唯一形态：与 ItsMyGo 联合部署**（单栈启动与自用页面已于 2026-11-06 移除）。
> README 只留几行命令；端口、初始化细节、联合拓扑放这里。

## 一键启动（联合）

前置：ItsMyGo 仓库与本仓库同级（`../ItsMyGo`），或设 `ITS_MYGO_DIR` / 按提示输入路径。

首次运行 `./deploy.sh` 会生成 `.env`，并提示还缺哪些模型密钥。编辑
`.env` 里的 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `EMBEDDING_API_KEY`
（`MINERU_API_TOKEN` 只有导入 PDF 时才需要），再执行一次：

```bash
./deploy.sh
```

脚本会：

1. 检查模型密钥，缺了直接提示；
2. 生成 / 复用共享密钥，写入两边 `.env`；
3. 先生起 ItsMyGo（创建 `its-my-go-net`）；
4. 再起本栈并接入共享网络，等待 `agent-key-init` 完成。

**本仓库不提供任何页面**：聊天、知识库导入、评测台全部从**平台侧**进入
（`http://localhost:<ItsMyGo 前端端口>`，默认 8088）。Java 侧地址：
`rag-agent:8003`（gRPC）、`rag-import:8001`（导入管理端）。
停止用 `./deploy.sh down`（停两套）。PowerShell 用 `./deploy.ps1`，参数相同。

## 命令

| 命令 | 作用 |
|---|---|
| `./deploy.sh` / `./deploy.sh joint` | 启动 ItsMyGo + rag_helper 联合栈 |
| `./deploy.sh down` | 停止两套（保留数据） |
| `./deploy.sh logs` | 跟踪 agent 日志 |
| `./deploy.sh status` | 查看 agent 容器状态 |
| `./deploy.sh reset` | 删除 agent 数据卷（需输入 `yes`） |

> 兼容别名：`up` 等同 `joint`、`joint-down` 等同 `down`（旧习惯不报错）。
> 单栈模式（`./deploy.sh up` 只起 agent）**已移除**。

**npm 入口**（仓库根 `package.json` + `scripts/npm-deploy.mjs`，零依赖，只做跨平台转发；
真正的逻辑仍在 `deploy.sh` / `deploy.ps1`）：

| npm 命令 | 等价 |
|---|---|
| `npm run deploy` / `npm run deploy:joint` | `./deploy.sh joint` |
| `npm run deploy:down` | `./deploy.sh down` |
| `npm run deploy:logs` | `./deploy.sh logs` |
| `npm run deploy:status` | `./deploy.sh status` |
| `npm run deploy:reset` | `./deploy.sh reset` |
| `npm run deploy:help` | 查看命令 |

需要 Node ≥18；不需要 `npm install`。退出码与底层脚本一致，`logs` / `status` 在 Docker
没启动时会明确报错（bash 与 PowerShell 行为已对齐）。

## 端口

| 服务 | 默认 | `.env` 变量 | 说明 |
|---|---|---|---|
| 知识库导入 | 8001 | `RAG_IMPORT_PORT` | 管理端 HTTP（Java 代理） |
| HTTP / SSE | 8002 | `RAG_HTTP_PORT` | 评测接口（Java 代理）+ 会话/对话调试接口 |
| gRPC | 8003 | `RAG_GRPC_PORT` | Java 侧调用入口 |
| MinIO API / 控制台 | 9002 / 9003 | `RAG_MINIO_PORT` / `RAG_MINIO_CONSOLE_PORT` | |
| Milvus / metrics | 19530 / 9091 | `RAG_MILVUS_PORT` / `RAG_MILVUS_METRICS_PORT` | |
| MongoDB / Redis | 27018 / 6380 | `RAG_MONGO_PORT` / `RAG_REDIS_PORT` | |

浏览器访问知识库图片的地址由 `RAG_MINIO_PUBLIC_ENDPOINT` 控制（默认
`127.0.0.1:9002`，**不带 `http://`**）。内网部署改成 `<服务器IP>:9002`。

## 首次启动做了什么

1. 检查 `.env` 的模型密钥，缺了直接提示；
2. 生成 64 位十六进制的 `AGENT_GRPC_KEY`，写入 `.env` 并同步到 ItsMyGo 的 `.env`；
3. `minio-init` 建 `rag-helper-knowledge-files` 与 `rag-helper-milvus-binlog`；
4. `collections-init` 建 `rag_helper` 库、`kb_chunks`、`kb_item_names`；
5. `agent-key-init` 把 `AGENT_GRPC_KEY` 幂等写入 MongoDB。

Neo4j 默认不启动。需要知识图谱：

```bash
docker compose --profile graph up -d
```

## 联合拓扑

- ItsMyGo 前端：`http://localhost:<FRONTEND_PORT>`（默认 8088）——**唯一入口**；
- Java → agent：`rag-agent:8003`（gRPC）、`rag-import:8001`（导入管理端）、
  `rag-chat:8002`（评测接口代理）；
- agent → Java：`its-my-go-backend:8080`；
- agent 访问自己的中间件使用 `rag-minio` / `rag-redis` / `rag-mongo` / `rag-milvus`
  唯一别名，避免与 ItsMyGo 的 `minio` / `redis` 服务名冲突。

## Docker Hub 拉不动

先用国内镜像站拉基础镜像，再打成标准名：

```bash
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
```

需要：`python:3.12-slim`、`mongo:7`、`redis:7-alpine`、
`minio/minio:latest`、`milvusdb/milvus:v2.5.4`、`quay.io/coreos/etcd:v3.5.18`；
Neo4j 启用时再加 `neo4j:5.26`。uv 由 Dockerfile 通过 pip 安装，不再依赖 ghcr；
国内网络可在 `.env` 设 `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`。

## 本地 venv 开发

不用 Docker 跑应用时，依赖容器用 `docker/milvus-compose.yml` 等启动，
三个服务用 `.venv/Scripts/python.exe -m ...` 起（本仓库已无页面，本地只用于开发与调试）。
完整命令见 [HANDOFF.md 的快速开始](../HANDOFF.md)。
