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

> **Neo4j 知识图谱是可选组件**：默认 `NEO4J_ENABLED=false`，不启动容器、
> 不走图谱导入/检索；需要时在 `.env` 打开并启动 `docker/neo4j-compose.yml`。
> 两个仓共用的开发基础设施（ItsMyGo `dev-infra/`）里，`neo4j` 也在 `kg` profile 下默认不启动。

**买小购**是一次买够平台的客服智能体：产品使用文档（说明书、用户手册）的导入与问答系统，
LangGraph 多路检索 RAG + deepagents 主 agent 编排，检索图降为 subagent，
经 gRPC 接入一次买够平台（Java 侧 ItsMyGo）。

仓库与包名仍为 `rag_helper` / `rag-helper`，不随产品名变更。

**运行形态**：**只支持与 ItsMyGo 平台联合启动**（`./deploy.sh joint`）。本仓库不提供页面，
界面全部在平台侧；单栈启动模式已移除。

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
| 对外接入 | **gRPC**（对 Java 平台，契约 `proto/agent.proto`）；管理端 HTTP（导入 / 评测，由 Java 代理） |
| Web | FastAPI（只提供 HTTP API，**无前端页面**） |
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

## Why deepagents?

选它不是为了“多智能体”这个概念，而是因为本项目正好是“一个对用户说话的主 agent + 多个领域专家”的形状，deepagents 把这类编排的成本压得很低（当前 `deepagents 0.7.23`）：

- **子 agent 注册表统一异构能力**：`CompiledSubAgent` 把现成的 LangGraph RAG 图直接挂成 `product-kb`；声明式 `SubAgent` 用 `name / description / system_prompt / tools` 接入 `product-guide`、`order-service`。主 agent 只通过 `task` 选择子 agent，不写“问说明书走哪条、问订单走哪条”的领域路由。见 [main_agent.py:294-371](app/agent/runtime/main_agent.py#L294-L371)。
- **上下文隔离**：`task` 默认 isolated，子 agent 只收到任务说明；RAG chunk、商品列表、工具调用历史不会回灌主对话，主 agent 只拿最终结论，多轮客服的 token 和干扰更可控。
- **编排胶水少，业务控制面仍归自己**：工具循环、子 agent 生命周期、消息回传、最后回答抽取由框架处理；[build_main_agent()](app/agent/runtime/main_agent.py#L388-L407) 约 20 行接完 3 个子 agent，输入护栏、表单、票据、usage 记账、Mongo 历史、SSE 映射仍在业务层。
- **自带工程化中间件**：文件工具、长会话摘要、工具调用修复、prompt caching、`general-purpose` 兜底子 agent 默认可用，减少长对话和模型异常调用的坑。
- **LangGraph 流式兼容**：子 agent 本质仍是 LangGraph 图，`stream(subgraphs=True)` 能把子图事件冒泡到主流程；项目据此把“派发 → 子 agent 执行 → 工具调用”映射成执行过程与 SSE trace。
- **扩展路径清晰**：后续要做并行派发、人工审批、skills / memory，框架接口已经预留，不需要更换编排层。

边界也要说清楚：`task` 默认一次一个子 agent，**不并行**；isolated 模式依赖主 agent 把任务描述写清楚；默认文件工具和兜底子 agent 扩大了能力面，需要用提示词/配置约束；它不是 workflow 引擎，复杂 DAG、并行聚合仍要自己用 LangGraph 搭。

## 功能介绍

> 面向顾客的能力都在平台客服对话里使用；细节实现与坑见 [HANDOFF.md](HANDOFF.md) 对应小节。

| 功能 | 说明 |
|---|---|
| **产品知识问答** | 查产品说明书 / 用户手册：怎么用、怎么装、故障怎么处理、参数是什么。多路检索（向量 / HyDE / 图谱 / 联网）→ RRF 融合 → 重排 → 充分性判定（不足则有界改写再检，最多 2 轮）；答案带配图引用，知识库没有就直说「没查到」并记录知识缺口，**不用通用知识编操作步骤**。 |
| **联网补充** | 知识库不足时可联网搜索；本地手册优先，网络内容只作补充。顾客可在客服设置里关闭联网。 |
| **AI 导购** | 按关键词 / 分类 / 价格搜在售商品（价格、库存、销量、评分、店铺、SKU），关键词不足自动语义检索；商品卡随回复与历史返回，按最终回复实际引用裁剪。 |
| **订单与物流查询** | 查当前登录用户**自己的**真实订单与物流（订单号 / 状态 / 商品 / 金额 / 物流状态与预计送达）；票据由 Java 每轮签发，不暴露收货人、电话、地址等 PII。 |
| **下单 / 退款 / 催办草稿** | 准备下单、退款申请、催发货、催退款草稿；**只 prepare 不执行**，用户必须在聊天确认卡片点确认；支持多单批量草稿（一次 ≤10 单、逐张确认）；动作前预检（在售 / SKU / 库存 / 地址 / 订单状态），不通过就不创建草稿。工具失败区分「未授权 / 暂时故障 / 业务拒绝」，只读调用有限重试、写操作不重试。 |
| **会话内表单** | 退款原因、修改收货地址以结构化表单下发；`formResult` 随下一轮回传后服务端**直接调工具**（不猜参数），`form_id` 双端一次性消费、过期拒收；历史里的表单只读。 |
| **客服设置** | 客服面板右上角设置：**智能推广 / 联网搜索 / 流式输出**三个开关，按用户持久化；流式为真增量（token 级 `delta`），关闭时只推整段 final。 |
| **智能推广** | 用户开启后，**登录后 0~5 分钟内随机延迟**推 1 条推荐（降价幅度 / 买过相似 / 销量 / 评分综合打分，**一次最多 3 件商品**，每天最多 1 条）；**写进客服智能体会话历史（带多张商品卡）**，不进顾客—店铺聊天，不新建通知表。 |
| **个人消费报告** | 按月/按年汇总已支付订单、实付/退款/净支出、优惠与均价，以及品类/店铺/商品 Top3 和月度趋势；平台有独立页面（`/user/report`），也可在客服对话里让智能体直接摘要。 |
| **会话体验** | 多用户会话隔离（`chat_message` 带 `user_id`/`tenant_id`，gRPC 按 `x-user-id` 过滤）；历史回放含执行流、配图、商品卡与只读表单；执行过程只显示中文动作，不暴露工具名/参数/返回原文；同一问题以**首次回答为锚点**保证多次回答口径一致；支持暂停/停止本轮。 |
| **客服话术库** | 独立 Milvus 集合 `kb_tone_faq`（18 条场景种子）；用户有情绪或涉及退款/物流/投诉/议价时，主 agent 先检索 2~3 条参考话术，学语气与结构再回答（不照抄、不提话术库）。 |
| **平台政策问答** | 独立**平台政策智能体**：用户协议、隐私、退换货与售后、商家规范以 MySQL 章节原文为准，Milvus `kb_platform_policy` 做语义召回；回答注明政策标题与章节、只引用命中原文、不扩大承诺；管理端可分库查看政策章节。 |
| **知识库管理 / 评测** | 文档导入（PDF/Markdown，含图片与图谱）、平台政策 Markdown 上传（写 MySQL 后异步重建章节向量）、知识缺口聚合与 FAQ 草稿、LLM-as-Judge 评测；均为管理端行为，由平台（ItsMyGo）代理调用，本仓库不提供页面。 |

## 快速启动（与 ItsMyGo 联合部署）

> **联合启动/停止入口已收归 ItsMyGo 仓库**（2026-11-07）：
> 演示环境用 ItsMyGo 的 `./deploy.sh joint` / `npm run deploy:joint`；
> 开发环境用 ItsMyGo 的 `npm run dev:joint`。
> 本仓库的 `deploy.sh` / `deploy.ps1` 只做**轻量转发**（`joint`/`down` 转给 ItsMyGo），
> `logs` / `status` / `reset` 仍管理本仓自己的容器。
> 需要 ItsMyGo 仓库位置：`ITS_MYGO_DIR` 环境变量 / 本仓 `.itsmygo-dir` 文件 / 默认同级 `../ItsMyGo`。

首次运行前先准备 `.env`（可复制 `.env.example`），填好
`OPENAI_API_KEY` / `DEEPSEEK_API_KEY` / `EMBEDDING_API_KEY`
（`MINERU_API_TOKEN` 只有导入 PDF 时才需要）。

```bash
# 在 ItsMyGo 仓库：
./deploy.sh joint            # 演示环境联合启动（Windows: .\deploy.ps1 joint）
npm run dev:joint            # 开发环境联合启动（中间件 + 两边应用）
npm run deploy:joint         # 演示环境 npm 入口
```

启动后**所有界面都在平台侧**（`http://localhost:<ItsMyGo 前端端口>`，默认 8088）：
聊天、知识库导入、评测台均从平台进入。gRPC 在 `rag-agent:8003`（Java 侧）。
停止两套：`./deploy.sh joint-down`。

端口、首次初始化、Neo4j、镜像源等问题见 [docs/deployment.md](docs/deployment.md)。
本地 venv 开发方式见 [HANDOFF.md](HANDOFF.md) 的快速开始。

## 进度

| 模块 | 状态 |
|---|---|
| 核心链路：导入 8 节点 / 检索 10 节点 / 主 agent + 3 个 subagent（检索循环默认关闭） | ✅ |
| 电商客服：订单物流、AI 导购、下单退款催办草稿、批量与动作前预检 | ✅ |
| 客服体验：客服设置、智能推广、消费报告、会话内表单、客服话术库、平台政策问答 | ✅ |
| 联合部署：唯一启动形态（`./deploy.sh joint`；gRPC + 管理端 HTTP） | ✅ |
| 企业化改造（8/16）与评估（P0/P1/P3 MVP，P5 部分） | 🟡 部分完成 |

历史里程碑与具体日期见 [HANDOFF.md](HANDOFF.md)；需求与范围见 [docs/requirements.md](docs/requirements.md)，
技术选型见 [docs/architecture.md](docs/architecture.md)。

P3 LLM-as-Judge 用法：`python -m app.eval.judge check` / `run --limit 3`，
结果写到 `output/eval/`；管理端从平台 `/admin/agent-eval` 进入（本仓库不提供页面）。
管理端还可以新建自建用例（保存到 `output/eval/cases_custom.yaml`，与内置用例合并）。

## 文档

| 文件 | 作用 |
|---|---|
| [docs/requirements.md](docs/requirements.md) | **需求分析** —— 产品定位、场景、功能/非功能需求、平台对接前提、验收标准、范围外、里程碑 |
| [docs/architecture.md](docs/architecture.md) | **架构选型** —— 技术栈选型与理由、总体架构、模块划分、契约概览、关键取舍、决策记录 D1–D16 |
| [docs/structure.md](docs/structure.md) | **代码与目录结构规范** —— 目标目录、依赖方向、命名与测试规则、迁移阶段 |
| [implementation.md](implementation.md) | **技术详细实现** —— 导入 / 检索 / 主 agent / 服务 / 存储 / 平台设施 / 测试评估的实现机制与代价 |
| [HANDOFF.md](HANDOFF.md) | **状态、坑与操作的权威源** —— 具体进度、下一步、踩过的坑；**§8 是设计与操作详档** |
| [evaluation-plan.md](evaluation-plan.md) | **评估方案** —— 评估对象、指标体系、测试集设计、方法矩阵、落地阶段 |
| [docs/deployment.md](docs/deployment.md) | **部署细节** —— 端口、首次初始化、联合部署、Neo4j、镜像源 |
| [docs/form-protocol-review.md](docs/form-protocol-review.md) | **已评审并落地** —— 会话内表单协议（proto 草案、Java 侧改动清单、7 个决策点及结论、落地记录） |
| [CLAUDE.md](CLAUDE.md) | 给 AI 助手的项目约定与协作偏好 |

> **文档分层**：需求看 requirements、选型看 architecture、「怎么做」看 implementation、
> 「现在什么状态、哪里有坑」看 HANDOFF、契约源码看 `proto/agent.proto`。
> 若有出入，**状态与坑一律以 [HANDOFF.md](HANDOFF.md) 为准**，需求以 requirements 为准，
> 契约以 proto 为准。
