# 掌柜智库 · 产品文档知识库 RAG

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
检索图：产品确认 → 四路并行检索（向量 / HyDE / 图谱 / 联网）→ RRF 融合 → 重排 → 生成答案

进化后（当前形态）：
浏览器 → Java 单体（ItsMyGo）──gRPC──> Agent 服务（单实例）
                                        主 agent（deepagents）· 唯一对用户说话的「嘴」
                                          ├─ subagent product-kb   （现有检索图）
                                          └─ subagent order-query  （查订单，模拟实现）
```

## 部署方式

**全本地 Docker**：依赖 8 个容器（Milvus ×2 / MinIO / MongoDB / mongo-express /
Neo4j / Redis / Attu），复用本机已有容器、不另起。
**启动顺序、全部命令与 .env 模板见 [HANDOFF.md](HANDOFF.md) §8.3（快速开始）。**

## 进度

| 阶段 | 状态 |
|---|---|
| 导入链路（8 节点） · 检索链路（8 节点） | ✅ 已完成 |
| 进化 M0–M5（统一询问入口 / subagent / HTTP + gRPC / 执行流界面） | ✅ 全部完成（2026-10-14，一个月验收 C 档五项就绪） |
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
