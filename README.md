# 掌柜智库 · 产品文档知识库 RAG

[![Tests](https://github.com/Tsuki11111/rag_helper/actions/workflows/test.yml/badge.svg)](https://github.com/Tsuki11111/rag_helper/actions/workflows/test.yml)
[![Stars](https://img.shields.io/github/stars/Tsuki11111/rag_helper?style=social)](https://github.com/Tsuki11111/rag_helper/stargazers)
[![Last commit](https://img.shields.io/github/last-commit/Tsuki11111/rag_helper)](https://github.com/Tsuki11111/rag_helper/commits/master)

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-1C3C3C?logo=langchain&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)
![Milvus](https://img.shields.io/badge/Milvus-2.5-00A1EA)
![MongoDB](https://img.shields.io/badge/MongoDB-会话与账本-47A248?logo=mongodb&logoColor=white)
![Neo4j](https://img.shields.io/badge/Neo4j-知识图谱-4581C3?logo=neo4j&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)
![MinIO](https://img.shields.io/badge/MinIO-对象存储-C72E49?logo=minio&logoColor=white)

产品使用文档（说明书、用户手册）的导入与问答系统：LangGraph 多路检索 RAG，
已进化为**网购平台智能客服** —— deepagents 主 agent 编排、检索图降为 subagent，
经 gRPC 接入 Java 网购平台（ItsMyGo）。

**定位**：自用与学习练手，全本地 Docker 运行 —— 非真实运营。

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | 主 agent：**deepagents**（LangGraph 之上）；检索图：LangGraph |
| 大模型 | 主 agent：**DeepSeek 官方 API**（`deepseek-flash`，直连）；检索链路：通义千问（OpenAI 兼容接口，`langchain-openai`） |
| 嵌入 | DashScope `text-embedding-v2`（1536 维，仅稠密向量） |
| 重排 | DashScope `gte-rerank-v2`（原生端点，非 OpenAI 兼容） |
| 向量库 | Milvus 2.5（standalone） |
| 对象存储 | MinIO |
| 会话历史 / 去重 / 账本 / 检查点 | MongoDB |
| 任务状态 / 暂停标志 | Redis 7（进度、结果、停止标记的跨进程共享存储；不可用时降级为进程内存） |
| 知识图谱 | Neo4j 5.26（Community，本地 Docker） |
| 图谱可视化 | ECharts 5.4.3（CDN 引入，三源回退 + 文本降级） |
| PDF 解析 | MinerU（云端 API） |
| 联网搜索 | 百炼 MCP `EnhancedSearch`（openai-agents 客户端，**`mcp<2`**） |
| 对外接入 | **gRPC**（对 Java 平台，契约 `proto/agent.proto`）；HTTP/SSE 供自用页面 |
| Web | FastAPI + 原生 HTML/JS（无框架） |
| 包管理 | uv（Python ≥ 3.12） |

## 架构

```
导入图：PDF/MD → 解析 → 读图 → 切分 → 产品主体识别 → 向量化 → 入 Milvus
检索图：产品确认 → 四路并行检索（向量 / HyDE / 图谱 / 联网）→ RRF 融合 → 重排
        → 充分性判定 →（不足则改写检索词再来一轮，最多 2 轮）→ 生成答案

进化后（当前形态）：
浏览器 → Java 单体（ItsMyGo）──gRPC──> Agent 服务（单实例）
                                        主 agent（deepagents）· 唯一对用户说话的「嘴」
                                          ├─ subagent product-kb   （现有检索图）
                                          └─ subagent order-query  （查订单，模拟实现）
```

## 部署方式

**全本地 Docker**：依赖 8 个容器（Milvus ×2 / MinIO / MongoDB / mongo-express /
Neo4j / Redis / Attu），复用本机已有容器、不另起。

**精简版快速开始**。完整命令、卷 / 桶命名与 Git Bash 的 `MSYS_NO_PATHCONV` 坑，
**一律以 [HANDOFF.md](HANDOFF.md) §8.3（快速开始）为准**；`.env` 模板见仓库根
[.env.example](.env.example)（同样以 §8.3 为准）。

1. **装依赖**：`uv sync`
2. **配环境**：`cp .env.example .env`，把几处 `sk-xxx` 填上（百炼 / DeepSeek / MinerU 的密钥，含义见文件内注释）
3. **起依赖容器**（顺序有讲究：**先 MinIO，再 Milvus**）：
   - 三个走 compose：

     ```bash
     docker compose -f docker/milvus-compose.yml up -d    # Milvus（含 etcd）
     docker compose -f docker/neo4j-compose.yml up -d     # Neo4j
     docker compose -f docker/redis-compose.yml up -d     # Redis
     ```

   - 四个是 `docker run` 起的（MinIO / MongoDB / mongo-express / Attu），命令见 §8.3；
     **本机重启后**一键拉回：`docker start minio milvus-etcd milvus-standalone attu mongo mongo-express neo4j redis`
4. **首次**建 Milvus 集合：`.venv/Scripts/python.exe -m app.import_process.agent.create_collections`
5. **起服务**（两个终端窗口）：
   - 导入服务 · 8001 · `http://127.0.0.1:8001/import.html` ——
     `.venv/Scripts/python.exe -m app.import_process.api.file_import_service`
   - 问答服务 · 8002 · `http://127.0.0.1:8002/chat.html` ——
     `.venv/Scripts/python.exe -m app.agent.api.query_service`

## 进度

| 阶段 | 状态 |
|---|---|
| 导入链路（8 节点） · 检索链路（8 节点） | ✅ 已完成 |
| 进化 M0–M5（统一询问入口 / subagent / HTTP + gRPC / 执行流界面） | ✅ 全部完成（2026-10-14，一个月验收 C 档五项就绪） |
| 检索循环（有界自纠正，默认关闭） | ✅ 已完成（2026-10-20；`RETRIEVAL_MAX_ROUNDS=2` 开启，见 HANDOFF §3.52） |
| 企业化改造（16 项） | 已完成 8 项（Phase 1 / 2） |
| 评估路线（6 阶段） | 已完成 P0 / P1，P5 部分 |

里程碑与排期的唯一权威见 [evolution-plan.md](evolution-plan.md)。

## 文档

| 文件 | 作用 |
|---|---|
| [HANDOFF.md](HANDOFF.md) | **状态、坑与约定的权威源** —— 具体进度、下一步、踩过的坑；**§8 是设计与操作详档**（原 README 主体） |
| [evolution-plan.md](evolution-plan.md) | **进化路线的唯一权威** —— 目标形态、里程碑排期、gRPC 契约与平台对接前提 |
| [docs/decisions-evolution.md](docs/decisions-evolution.md) | 决策记录 —— 每条路线决策的结论与理由 |
| [CLAUDE.md](CLAUDE.md) | 给 AI 助手的项目约定与协作偏好 |

> 本 README 是项目门面（技术栈 / 架构 / 部署 / 进度）；**一切细节以
> [HANDOFF.md](HANDOFF.md) 为准** —— 两者允许内容重复，若有出入，一律以 HANDOFF 为准
> （路线问题以 evolution-plan 为准）。
