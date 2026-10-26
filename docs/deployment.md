# 部署说明 · rag_helper

> README 只留三行命令；端口、初始化细节、联合拓扑放这里。

## 一键启动

首次运行 `./deploy.sh` 会生成 `.env`，并提示还缺哪些模型密钥。编辑
`.env` 里的 `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `EMBEDDING_API_KEY`
（`MINERU_API_TOKEN` 只有导入 PDF 时才需要），再执行一次：

```bash
./deploy.sh
```

打开：

- 知识库导入：`http://localhost:8001/import.html`
- 聊天页面：`http://localhost:8002/chat.html`
- 评测台：`http://localhost:8002/eval.html`（与聊天页共用访问密钥）
- gRPC（Java 调用）：`localhost:8003`

`./deploy.sh` 会自动生成 `AGENT_GRPC_KEY`，并创建 MinIO 桶、Milvus 库和集合。
停止用 `./deploy.sh down`。

PowerShell 用 `./deploy.ps1`，参数相同。

## 命令

| 命令 | 作用 |
|---|---|
| `./deploy.sh` | 启动 agent 单栈 |
| `./deploy.sh joint` | 启动 ItsMyGo + rag_helper 联合栈 |
| `./deploy.sh down` | 停止 agent 单栈（保留数据） |
| `./deploy.sh joint-down` | 停止两套（保留数据） |
| `./deploy.sh logs` | 跟踪 agent 日志 |
| `./deploy.sh status` | 查看容器状态 |
| `./deploy.sh reset` | 删除 agent 数据卷（需输入 `yes`） |

## 端口

| 服务 | 默认 | `.env` 变量 |
|---|---|---|
| 知识库导入 | 8001 | `RAG_IMPORT_PORT` |
| HTTP / SSE | 8002 | `RAG_HTTP_PORT` |
| gRPC | 8003 | `RAG_GRPC_PORT` |
| MinIO API / 控制台 | 9002 / 9003 | `RAG_MINIO_PORT` / `RAG_MINIO_CONSOLE_PORT` |
| Milvus / metrics | 19530 / 9091 | `RAG_MILVUS_PORT` / `RAG_MILVUS_METRICS_PORT` |
| MongoDB / Redis | 27018 / 6380 | `RAG_MONGO_PORT` / `RAG_REDIS_PORT` |

浏览器访问知识库图片的地址由 `RAG_MINIO_PUBLIC_ENDPOINT` 控制（默认
`127.0.0.1:9002`，**不带 `http://`**）。内网部署改成 `<服务器IP>:9002`。

## 首次启动做了什么

1. 检查 `.env` 的模型密钥，缺了直接提示；
2. 生成 64 位十六进制的 `AGENT_GRPC_KEY`，写入 `.env`；
3. `minio-init` 建 `rag-helper-knowledge-files` 与 `rag-helper-milvus-binlog`；
4. `collections-init` 建 `rag_helper` 库、`kb_chunks`、`kb_item_names`；
5. `agent-key-init` 把 `AGENT_GRPC_KEY` 幂等写入 MongoDB。

Neo4j 默认不启动。需要知识图谱：

```bash
docker compose --profile graph up -d
```

## 联合 ItsMyGo

两个仓库放成同级（`ItsMyGo/`、`rag_helper/`），执行：

```bash
./deploy.sh joint
```

如果不在同级，脚本会提示输入 ItsMyGo 目录，也可以提前设置 `ITS_MYGO_DIR`。
脚本会自动：

- 生成 / 复用共享密钥并写入两边 `.env`；
- 先启动 ItsMyGo（创建 `its-my-go-net`）；
- 再启动 agent 并接入共享网络。

Java 侧地址：`rag-agent:8003`、`rag-import:8001`；
agent 回调 Java：`its-my-go-backend:8080`。
agent 访问自己的中间件使用 `rag-minio` / `rag-redis` / `rag-mongo` / `rag-milvus`
唯一别名，避免与 ItsMyGo 的 `minio` / `redis` 服务名冲突。
停止两套：`./deploy.sh joint-down`。

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
三个服务用 `.venv/Scripts/python.exe -m ...` 起。完整命令见
[HANDOFF.md 的快速开始](../HANDOFF.md)。
