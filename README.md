# 买小购 · 一次买够平台客服智能体

[![Tests](https://github.com/Tsuki11111/rag_helper/actions/workflows/test.yml/badge.svg)](https://github.com/Tsuki11111/rag_helper/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
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

**买小购**是一次买够平台的客服智能体：产品使用文档（说明书、用户手册）的导入与问答系统，
LangGraph 多路检索 RAG + deepagents 主 agent 编排，检索图降为 subagent，
经 gRPC 接入一次买够平台（Java 侧 ItsMyGo）。

仓库与包名仍为 `rag_helper` / `rag-helper`，不随产品名变更。

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
浏览器 → 一次买够平台（Java 单体 ItsMyGo）──gRPC──> 买小购 Agent 服务（单实例）
                                        主 agent（deepagents）· 唯一对用户说话的「嘴」
                                          ├─ subagent product-kb   （产品手册检索）
                                          ├─ subagent product-guide（商品目录 / 推荐 / 语义检索）
                                          └─ subagent order-service（订单 / 物流 / 地址 / 下单、退款、催发货与催退款草稿）
```

**多用户会话隔离**：`chat_message` 每条带 `user_id` / `tenant_id`，
`chat_session` 记录会话 owner；gRPC 的 Converse / Stop / GetHistory 校验归属，
ListSessions / GetHistory 按 `x-user-id` 过滤。HTTP 调试入口统一按 `local` 归属。
详见 [HANDOFF.md](HANDOFF.md) §3.54。

**订单查询（M7）**：order-service 不再返回模拟数据。工具凭 Java 每轮 Converse 签发的
短期票据，回调 `POST /internal/agent/orders/query` 查当前用户的真实订单
（订单号 / 状态 / 商品 / 金额 / 物流单号；不含收货人、电话、地址）。
**物流摘要（M7.5）**：订单工具还返回物流状态、预计送达时间、快递公司/快递员；
地址不进 agent 上下文。Java 侧地址由 `ITS_MYGO_BASE_URL` 配置，默认
`http://127.0.0.1:8080`。详见 [HANDOFF.md](HANDOFF.md) §3.58 / §3.61。

**智能下单 / 退款（M9/M10）**：order-service 子 agent 可以列出收货地址、准备订单草稿、
准备退款申请草稿。**工具只 prepare，不执行**；用户在 ItsMyGo 聊天里的确认卡片点击确认后，
Java 才真正下单 / 提交退款。工具返回固定写明「尚未下单 / 尚未提交」。
详见 [HANDOFF.md](HANDOFF.md) §3.59。

**催发货（M8）**：order-service 新增 `prepare_urge_shipment`，只对「待发货
（已支付且未发货）」的订单生成草稿；用户在确认卡片确认后，Java 触发 agent 执行图。
图先调用 Java 的发送工具（Java 校验并落库为 AGENT 站内消息），
再由买小购向用户回复「催发货已完成」并代商家致歉。
**草稿不等于已发送**，待收货（已发货）/ 待支付 / 退款中的订单不会被催。

**催退款（M8 二期）**：order-service 新增 `prepare_urge_refund`，只对「退款中
（REFUNDING）」订单生成草稿；确认后走 `refund_graph.py`，由 agent 调 Java 发送工具，
Java 重新校验仍为退款中后给商家发 AGENT 消息，agent 再回复「催退款已完成 + 致歉」。
已退款 / 已驳回 / 其他状态不会被催。

**AI 导购（M11–M13）**：新增 `product-guide` 子 agent，按关键词 / 分类 / 价格搜索
商品目录，返回价格、库存、销量、评分、店铺和 SKU；关键词不足时自动语义检索
（DashScope embedding + 内存余弦，TTL 300s）。商品卡随 final / 历史返回，
主 agent 收尾时按最终回复引用裁剪，避免把中间候选全挂上去。
详见 [HANDOFF.md](HANDOFF.md) §3.60。

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
| M7 真实订单查询（agent 回调 Java 内部接口） | ✅ 已完成（2026-10-21，见 HANDOFF §3.58） |
| M9/M10 智能下单与退款申请（只生成草稿，用户确认后 Java 执行） | ✅ 已完成（2026-10-21，见 HANDOFF §3.59） |
| M8 催发货（只生成草稿，用户确认后 Java 发站内消息） | ✅ 已完成（2026-10-22，见 HANDOFF §3.63） |
| M8 催退款（仅 REFUNDING，agent 执行图 + Java 发站内消息） | ✅ 已完成（2026-10-23，见 HANDOFF §3.64） |
| M11–M13 AI 导购 / 语义检索 / 商品卡 | ✅ 已完成（2026-10-21，见 HANDOFF §3.60） |
| M7.5 订单物流摘要 | ✅ 已完成（2026-10-21，见 HANDOFF §3.61） |
| 企业化改造（16 项） | 已完成 8 项（Phase 1 / 2） |
| 评估路线（6 阶段） | 已完成 P0 / P1，P5 部分 |

里程碑与排期的唯一权威见 [evolution-plan.md](evolution-plan.md)。

## 文档

| 文件 | 作用 |
|---|---|
| [HANDOFF.md](HANDOFF.md) | **状态、坑与约定的权威源** —— 具体进度、下一步、踩过的坑；**§8 是设计与操作详档**（原 README 主体） |
| [implementation.md](implementation.md) | **技术实现详解** —— 导入 / 检索 / 主 agent / 服务 / 存储 / 平台设施 / 测试评估的实现机制与取舍 |
| [evolution-plan.md](evolution-plan.md) | **进化路线的唯一权威** —— 目标形态、里程碑排期、gRPC 契约与平台对接前提 |
| [docs/decisions-evolution.md](docs/decisions-evolution.md) | 决策记录 —— 每条路线决策的结论与理由 |
| [CLAUDE.md](CLAUDE.md) | 给 AI 助手的项目约定与协作偏好 |

> 本 README 是项目门面（技术栈 / 架构 / 部署 / 进度）；**一切细节以
> [HANDOFF.md](HANDOFF.md) 为准** —— 两者允许内容重复，若有出入，一律以 HANDOFF 为准
> （路线问题以 evolution-plan 为准）。
