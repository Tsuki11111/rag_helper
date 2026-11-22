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
> 不走图谱导入/检索；需要时在 `.env` 打开，并在 ItsMyGo/dev-infra 里启动 neo4j（`--profile kg`）。
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
| 嵌入 | DashScope `qwen3.7-text-embedding`（1536 维，dense + sparse） |
| 重排 | DashScope `qwen3.7-text-rerank`（原生端点，非 OpenAI 兼容） |
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

### 1. 一张图看整体

系统由「两条 LangGraph 图 + 一层 deepagents 主 Agent + 一组受票据保护的 Java 工具」组成。浏览器始终只跟 ItsMyGo 说话，Agent 只在内网、只接受平台调用。

```
浏览器 / 管理端
      │  只访问 ItsMyGo（页面、登录、权限都在平台侧）
      ▼
ItsMyGo Java 单体（端口 8088）
      │  gRPC 8003：Converse / Stop / ListSessions / GetHistory
      │  HTTP 8001：导入 / 知识缺口；8002：评测 / 会话调试（由 Java 代理）
      ▼
买小购 Agent 服务（单实例）
      ├─ 主 Agent（deepagents，唯一对用户说话的「嘴」）
      │    ├─ subagent product-kb       产品手册检索（跑检索图）
      │    ├─ subagent product-guide    商品目录 / 推荐 / 语义检索
      │    ├─ subagent order-service    订单 / 物流 / 地址 / 下单退款催办草稿
      │    └─ subagent policy-agent     平台政策 / 售后规则
      ├─ 导入图（管理端触发：PDF/MD → 切分 → 向量化 → Milvus）
      └─ 工具回调 ──HTTP + X-Agent-Ticket──> ItsMyGo /internal/agent/**
```

三条边界：

1. **浏览器只跟 Java 说话**——Agent 服务不暴露公网，不保存用户登录态；
2. **Agent 只认平台密钥 + `X-User-Id`**，模型不参与身份判定；
3. **业务写操作只由 Java 执行**——Agent 最多 prepare 草稿，用户确认后 Java 校验并执行。

### 2. 导入图（8 节点）

```
PDF/MD
  → node_entry            文件校验 / 路由
  → node_pdf_to_md        PDF 调 MinerU 转 Markdown；MD 直接读
  → node_md_img           扫描图片、生成摘要、上传 MinIO、替换引用
  → node_document_split   按标题切分；长章节再切；短章节合并
  → node_item_name_recognition  识别产品主体（LLM + 规则守卫）
  → node_dashscope_embedding  DashScope qwen3.7-text-embedding（1536 维 dense + sparse）
  → node_import_milvus    按 file_title 幂等清理后写入 kb_chunks（稳定 chunk_key）
  → node_import_kg        可选：写入 Neo4j 图谱（默认关闭）
```

关键点：导入以 `file_title` 为幂等键，**不能按 `item_name` 清理**（不同文档可能描述同一产品）；切片主键是 `chunk_key`（文档 + 章节 + 章节内序号，不含正文），重建后引用不断链；导入侧保留检查点，失败可续跑。

### 3. 检索图（五路召回）

```
产品确认
  → 五路并行召回：向量（20）/ HyDE（10）/ BM25（10）/ 图谱（10）/ 联网
  → RRF 融合（留 20）
  → qwen3.7-text-rerank 重排（留 10）
  → 充分性判定（默认关；打开后每轮都判）
       ├─ 充分 → 生成答案（配图引用 / 证据不足则拒答）
       └─ 不足 → 换策略改写检索词，再检（最多 2 轮）
```

| 节点 | 做什么 |
|---|---|
| 产品确认 | 产品目录 BM25 + 别名/型号/部件规则做实体链接；LLM 只改写问题，不再从目录里猜产品；同手册多型号（如 HL-3040CN / HL-3070CW）按手册归一，默认步骤通用；缺规格先问全幅/半幅 |
| 向量召回 | Milvus 混合检索（dense COSINE + sparse IP，RRF）+ `item_name` 标量过滤；sparse 为空/异常回退稠密检索 |
| HyDE 召回 | 先生成假设性回答再检索，补语义鸿沟 |
| BM25 召回 | `kb_chunks` 的 `content + title` 内存 BM25（中文单字+二字、型号整体保留），作为第五路按 0.2 权重进 RRF；产品识别另走目录 BM25，索引缓存 300 秒，不改 Milvus schema |
| 图谱召回 | 可选 Neo4j 实体/关系多跳（`NEO4J_ENABLED=false` 时跳过） |
| 联网召回 | 百炼 MCP `EnhancedSearch`；本地手册优先，网络只作补充 |
| RRF 融合 | 多路结果按排名融合，不依赖单一召回 |
| 重排 | `qwen3.7-text-rerank` + 动态 Top-K（最多 10 条，断崖截断）+ 本地优先分区，压低噪声 |
| 充分性判定 | 判定证据够不够（覆盖实体/数值/步骤链）；不足才进入改写循环，缺什么会传给生成侧。**默认关闭**（`RETRIEVAL_MAX_ROUNDS=2` 打开） |
| 有界改写 | 最多 2 轮，换策略重检并带守卫防查询漂移；耗尽则用第一轮证据 + 明确说出缺什么 |
| 答案生成 | 只引用命中片段，带配图；知识库没有就直说「没查到」并记知识缺口；确需补充时只给**明确标注的通用建议**（固定开头、1–2 句，不写型号能力/参数） |
| 终稿接地 | 主 agent 转述后核对事实台账：价格/订单号/型号确定性核对 + `deepseek-flash` 校验/一次修复；fail-open，默认开 |

### 4. 主 Agent 与 Subagent

- 主 Agent 用 **deepagents**（LangGraph 之上）实现，是唯一对用户说话的出口；subagent 只执行任务、只回最终结果，不直接面向用户。
- `product-kb` 用 `CompiledSubAgent` 把现成的 LangGraph 检索图挂进来；`product-guide`、`order-service`、`policy-agent` 用声明式 `SubAgent`（`name / description / system_prompt / tools`）。
- `task` 默认 **isolated**：子 Agent 只收到任务说明，RAG chunk、商品列表、工具历史不回灌主对话；主 Agent 只拿结论，控制长会话 token 与干扰。
- 主 Agent 自己的工具循环、子 Agent 生命周期、消息回传由 deepagents 处理；输入护栏、表单、票据、usage 记账、Mongo 历史、SSE 映射仍在业务层。

### 5. 一轮 `Converse` 的生命周期

```
Java ──gRPC Converse(message, session_id)──> Agent
  1. 鉴权：metadata 的 authorization（平台密钥）+ x-user-id
     + x-agent-tool-ticket（Java 每轮签发的工具票据）
     + x-agent-internal-event（用户确认动作后的内部事件）
  2. 会话归属：ensure_session_owner(session_id, user_id, tenant_id)
  3. 输入护栏：去空白/零宽字符，匹配伪造 role、特殊 token、套 prompt 等；
     命中直接拒答，不烧模型调用
  4. usage_context：trace_id = run_id，整轮墙钟/ token 预算开始计时
  5. 读最近 10 条 chat_message（按 user_id 过滤）→ 组装主 Agent 入参
  6. deepagents 循环：主 Agent 决策 → 派发 subagent / 调工具
     - 工具经 X-Agent-Ticket 回调 Java /internal/agent/**
     - 查询类可有限重试；执行类只 prepare、不重试
  7. 事件流：SessionStart → Trace* → Delta* → Usage* → Final
  8. 收尾：写 chat_message、query_runs、llm_usage；停止标记按 run_id 清理
```

表单回传和内部事件走**短路分支**：`form_result` 直接调工具（不经过模型猜参数），`internal_event` 直接执行对应动作图，不重新跑一轮对话。

### 6. 状态与存储

| 存储 | 存什么 | 关键约束 |
|---|---|---|
| Milvus | `kb_chunks`、`kb_item_names`、`kb_platform_policy`、`kb_tone_faq` | 切片主键 `chunk_key`（稳定业务键）+ `item_name` 标量过滤；按 `file_title` 幂等清理；切片集合只存被读取的 12 个字段 |
| MongoDB | `chat_session`、`chat_message`、`query_runs`、`llm_usage`、LangGraph 检查点 | 检查点 TTL 7 天，`thread_id=run_id`；主 Agent 跨轮状态靠历史，不靠 checkpointer |
| Redis | 任务进度、`active` / `stop` 标记、工具票据 | 默认 TTL 86400；停止标记按 run_id 隔离；不可用时降级进程内存 |
| MinIO | 原始文件、知识库图片 | 桶名 `rag-helper-*`；图片 URL 由 `RAG_MINIO_PUBLIC_ENDPOINT` 控制 |
| Neo4j | 可选知识图谱 | 默认关闭；关闭是配置跳过，不是降级故障 |

### 7. 安全与写操作

- **身份**：Java 每轮签发 10 分钟工具票据（Redis），只放 `ContextVar`，**不进模型上下文**；工具回调用 `X-Agent-Ticket` 换身份。
- **数据**：订单/物流查询按 `x-user-id` 隔离，不暴露收货人、电话、地址等 PII。
- **写操作**：Agent 只生成 `PENDING_CONFIRM` 草稿；用户在 Java 聊天卡片确认后，Java 按服务端快照执行，Agent 无法指定用户或金额。
- **失败策略**：未授权 / 暂时故障 / 业务拒绝三分；只读最多重试 2 次，写操作永不重试；工具超时 5s。
- **可观测**：`run_id` 串起暂停令牌、日志 trace、`query_runs`、`llm_usage` 与检查点；`Trace` 只承载用户可读中文动作，不暴露工具名、参数和原文。
- **敏感主题**：主 agent 入口在输入注入护栏之后直接固定拒答，记 `rejected`，不派发 RAG 子图；与注入的 `blocked` 分开统计，避免把“有人来打”和“敏感合规”混成一类。
- **三条红线**：LLM/RAG 不直接写业务库；风控不用 LLM 决策；高危动作必须人工确认 + 审计。

### 8. 部署拓扑与端口

| 服务 | 默认端口 | 说明 |
|---|---|---|
| 知识库导入 / 知识缺口 | 8001 | 管理端 HTTP，Java 代理 |
| HTTP / SSE | 8002 | 评测接口 + 会话调试，Java 代理 |
| gRPC | 8003 | 平台唯一生产调用入口 |
| MinIO API / 控制台 | 9002 / 9003 | 原始文件与图片 |
| Milvus / metrics | 19530 / 9091 | 向量库 |
| MongoDB | 27018 | 会话、账本、检查点 |
| Redis | 6380 | 任务状态与停止标记 |

唯一启动形态是 `./deploy.sh joint`（与 ItsMyGo 联合）；本仓库没有页面，聊天、导入、评测入口都在平台侧。生产路径走 gRPC，HTTP/SSE 只保留作调试与管理端。

### 9. 明确边界

单实例（SSE 队列与任务状态在进程内，暂不能多 worker）；主 Agent 一轮没有 durable execution；没有 per-tenant 配额、限流、Prometheus/OTel/Jaeger；检查点只覆盖 LangGraph 图；Neo4j、检索循环都可配置关闭。完整限制见 [docs/architecture.md §7](docs/architecture.md#7-已知限制与技术债)。

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
| **产品知识问答** | 查产品说明书 / 用户手册：怎么用、怎么装、故障怎么处理、参数是什么。先做产品实体解析（目录 BM25 + 别名/型号/部件规则；同手册多型号按手册归一，缺规格先问全幅/半幅），再多路检索（向量 / HyDE / BM25 / 图谱 / 联网）→ RRF 融合 → 重排 → 充分性判定（不足则有界改写再检，最多 2 轮）；答案带配图引用，知识库没有就直说「没查到」并记录知识缺口，**不用通用知识编操作步骤**。 |
| **联网补充** | 知识库不足时可联网搜索；本地手册优先，网络内容只作补充。顾客可在客服设置里关闭联网。 |
| **AI 导购** | 先读用户画像（常买品类/品牌/价格带，冷启动跳过），再按关键词 / 分类 / 价格搜在售商品（价格、库存、销量、评分、店铺、SKU），关键词不足自动语义检索；商品卡随回复与历史返回，按最终回复实际引用裁剪，并按正文商品顺序穿插。 |
| **订单与物流查询** | 查当前登录用户**自己的**真实订单与物流（订单号 / 状态 / 商品 / 金额 / 物流状态与预计送达）；票据由 Java 每轮签发，不暴露收货人、电话、地址等 PII。 |
| **下单 / 退款 / 催办草稿** | 准备下单、退款申请、催发货、催退款草稿；**只 prepare 不执行**，用户必须在聊天确认卡片点确认；支持多单批量草稿（一次 ≤10 单、逐张确认）；动作前预检（在售 / SKU / 库存 / 地址 / 订单状态），不通过就不创建草稿。工具失败区分「未授权 / 暂时故障 / 业务拒绝」，只读调用有限重试、写操作不重试。 |
| **会话内表单** | 退款原因、修改收货地址以结构化表单下发；`formResult` 随下一轮回传后服务端**直接调工具**（不猜参数），`form_id` 双端一次性消费、过期拒收；历史里的表单只读。 |
| **安全与合规** | 敏感主题在主 agent 入口直接返回固定拒答并记 `rejected`，**不派发 RAG、不调主模型**；输入注入仍走 `blocked`，两者语义分开。管理端写接口仅 admin 可调，HTTP 会话按 `user_id` 做归属，上传文件名/撤回 `file_title` 做路径校验。 |
| **客服设置** | 客服面板右上角设置：**智能推广 / 联网搜索 / 流式输出**三个开关，按用户持久化；流式为真增量（token 级 `delta`），关闭时只推整段 final。 |
| **智能推广** | 用户开启后，**登录后 0~5 分钟内随机延迟**推 1 条推荐（降价幅度 / 买过相似 / 用户画像 / 销量 / 评分综合打分，同品类最多 2 件，**一次最多 3 件商品**，每天最多 1 条）；**写进客服智能体会话历史（带多张商品卡）**，不进顾客—店铺聊天，不新建通知表。 |
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
>
> 本仓也提供一份等价的开发联合启动包装：`npm run dev:joint`（脚本
> `scripts/dev-joint.mjs`），方便只打开 rag_helper 时直接拉起两边；官方入口仍是
> ItsMyGo 仓库的 `npm run dev:joint`。

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
| 核心链路：导入 8 节点 / 检索五路召回（向量 / HyDE / BM25 / 图谱 / 联网）/ 主 agent + 4 个 subagent（检索 / 导购 / 订单 / 平台政策；检索循环可配，默认关） | ✅ |
| 嵌入升级：qwen3.7-text-embedding（1536，dense + sparse），`kb_chunks` 混合检索并保留 dense 回退 | ✅ 2026-11-15 |
| 产品实体解析：目录 BM25 + 别名/型号/部件规则，LLM 只改写、不再猜产品；同手册多型号按手册归一；缺规格先问全幅/半幅 | ✅ 2026-11-16 / 多型号归一 2026-11-17 |
| 安全策略：敏感主题入口固定拒答、记 `rejected`、不派发 RAG；与输入注入 `blocked` 分开 | ✅ 2026-11-16 |
| 切片稳定身份：`kb_chunks` 以 `chunk_key`（doc + 章节 + 章节内序号）为主键，重建不再断评测 gold；schema 只留被读的 12 个字段，集合名不再带版本号 | ✅ 2026-11-17 |
| 受控补充建议：手册没写时给明确标注的通用建议，judge 用 `advisory` 与 `unsupported` 分开统计（幻觉率不含 advisory） | ✅ 2026-11-17 |
| 检索充分性判定 + 一次换策略补检（能力落地，实测无收益故**默认关**）；指代改写保住产品主体（`anaphora` 由 0/4 变 4/4 作答） | ✅ 2026-11-17 |
| 多轮续问：省略主语的追问（如「怎么进行烫金」）从任务描述补回历史型号，不再误判无产品 | ✅ 2026-11-18 |
| 多轮产品主体：主 agent 从真实历史生成结构化 `history_product`，覆盖纯中文产品名 | ✅ 2026-11-19 |
| 评测 gold 重映射（HL-3040 56→67 条新切片）+ 同批 3×20 重复与独立盲评校准 | ✅ 2026-11-19 |
| 召回图片按引用内联展示：前端块级打字机，图片跟到「图 N」所在行后 | ✅ 2026-11-19 |
| 章节路径：切分时维护标题层级，`section_path` 输出真实 `H1 > H2 > H3`，不改 `chunk_key` | ✅ 2026-11-19 |
| 生成策略按问题类型路由（步骤/事实/描述/通用），judge 以 core 幻觉为主口径 | ✅ 2026-11-21 |
| 提示词统一存放：subagent 描述/系统提示、judge/answer/rewrite 的 system prompt 全部外置到 `prompts/` | ✅ 2026-11-22 |
| 四个子智能体统一 `agent/api/application/domain/infrastructure` 分层目录，配置统一在 `app/conf/` | ✅ 2026-11-22 |
| 安全边界：上传/撤回路径校验、HTTP 会话 `user_id` 归属、管理端写接口 admin、镜像补 `scripts` | ✅ 2026-11-22 |
| 正确性批次：RAG 记账并入整轮、终稿只取本轮、Java 目录上限 100、导入原子性、产品名失败即 failed | ✅ 2026-11-22 |
| 结构治理：共享客户端上移 `app/clients`、order/policy 分层、main_agent 拆出流消费/对账、配置集中 `app/conf` | ✅ 2026-11-22 |
| 测试基建：pytest 接入 dev 依赖与 CI，`tests/unit|regression|integration` 分层；52 条 pytest + 122 条 regression | ✅ 2026-11-22 |
| 终稿接地：事实台账 + 高风险 token 确定性核对 + `deepseek-flash` 校验/一次修复；fail-open，默认开，每轮最多 +2 次调用 | ✅ 2026-11-18 |
| judge 幻觉口径拆分：unsupported / contradicted / core / claims-per-answer，旧 JSONL 兼容 | ✅ 2026-11-18 |
| 电商客服：订单物流、AI 导购、下单退款催办草稿、批量与动作前预检 | ✅ |
| 客服体验：客服设置、智能推广、消费报告、会话内表单、客服话术库、平台政策问答 | ✅ |
| 联合部署：唯一启动形态（`./deploy.sh joint`；gRPC + 管理端 HTTP） | ✅ |
| 代码结构：entrypoints / importing / agent runtime / eval 已统一，infra/shared 待迁 | 🟡 部分完成 |
| 企业化改造（8/16）与评估（P0/P1/P3 MVP，P5 部分） | 🟡 部分完成 |

> 2026-11-15：嵌入升级已落地；沿用旧卷 `rag_helper-milvus-data` 原地重建，四个集合共 610/9/18/49 条，MinIO 源文件与 Mongo 去重记录保留，详见 [HANDOFF.md](HANDOFF.md) §3.90。
>
> 2026-11-17：切片集合换成稳定主键 `chunk_key` 的版本（893 条），迁移期曾与新集合并存，
> 当天晚些已归一到单一名字 `kb_chunks`（旧的 auto_id 集合已删除）。
> 同日 judge 口径新增 `hallucination_claim_rate` / `advisory_claim_rate`，详见 [HANDOFF.md](HANDOFF.md) §3.94 / §3.95。
> judge 默认仍是 `deepseek-v4-pro`；想图便宜可设 `JUDGE_ALLOW_SAME_MODEL=1` 用 `deepseek-flash`
> 自评（指标不可与历史报告横比），见 [HANDOFF.md](HANDOFF.md) §3.96。
>
> 2026-11-17 全量 144 例（`kb_chunks` + flash 自评）：幻觉 claim 率 14.54%、幻觉答案率 74.59%、
> 证据覆盖率 85.07%、groundedness 均值 0.8515；分类与遗留问题（`anaphora` 4/4 拒答）见
> [HANDOFF.md](HANDOFF.md) §3.96。
>
> 2026-11-17 检索调参：向量路 `DENSE_TOP_K` 10→20、重排 `RERANK_MAX_TOPK` 10→5，
> 并给 HyDE 拆出独立参数 `HYDE_TOP_K`（仍 10）；离线模拟 Recall@5 由 @10 的 0.9158 降到 0.8452，
> 详见 [HANDOFF.md](HANDOFF.md) §3.97。**当晚重排上限改回 10**（8 例探针 0/8，收窄只丢证据不换收益），
> 向量路 20 保留，见 §3.102。
>
> 2026-11-17 生成上下文头部标注出处：`[i] 手册名 · 章节路径`（超 80 字符截断），
> 让多手册并存时模型分得清证据来源；内部标识字段不进上下文，见 [HANDOFF.md](HANDOFF.md) §3.98。
>
> 2026-11-17 schema 精简：`kb_chunks` 由 24 字段砍到 12 个（只留有读方的），重建 893 条
> 并复用旧 `chunk_id`（130 条 gold 不受影响）；证据扩展改按 `chunk_index` 排序，单切片上下文
> 上限 1200 → 2048，见 [HANDOFF.md](HANDOFF.md) §3.99。
>
> 2026-11-17 集合归一：删掉 `auto_id` 旧集合，`kb_chunks_v2` 的数据落到同名 `kb_chunks`
> （`scripts/consolidate_chunks_collection.py`，带 `--dry-run`），代码/配置里不再有 v2 字样，
> 见 [HANDOFF.md](HANDOFF.md) §3.100。
>
> 2026-11-17 幻觉率复测（归一后）：144 例、judge `deepseek-flash` 自评 —— claim 级 14.3%、
> 答案级 72.1%、groundedness 0.856；与上一轮差异在噪声内（逐例 35 降 / 40 升），
> 但 Recall 口径从 @10 变成 @5（0.866 → 0.758），见 [HANDOFF.md](HANDOFF.md) §3.101。
>
> 2026-11-18 降幻觉：归因发现严格答案级 78.6% 主要来自「长答案 × 任意一条无支撑」
> （平均 11.78 claim/答案，claim 级 15.5%，被证据明确否定的答案仅 8.7%）；新增
> `unsupported` / `contradicted` / `core` / `claims-per-answer` 拆分口径，并在主 agent
> 收尾加**终稿接地**（事实台账 + 高风险 token 核对 + flash 校验/一次修复，默认开，
> 每轮最多 +2 次调用，单次约 ¥0.002–0.003，校验+修复合计约 ¥0.005–0.007）。离线回归
> 89 通过 / 26 跳过 / 0 失败（Docker 未启动时）；真实依赖下 **114 通过 / 1 跳过 / 0 失败**，
> 见 [HANDOFF.md](HANDOFF.md) §3.103 / §3.104。
>
> 2026-11-18 真实 35 例同批 A/B（`dev-infra` + flash 自评）：严格答案级幻觉
> 83.9% → **61.3%**，claim 级 13.4% → **11.0%**，core claim 级 **5.2%**；
> 同价成本约 +10%、总延迟约 +9%。详见 [HANDOFF.md](HANDOFF.md) §3.104。
>
> 2026-11-19 技术债收尾：HL-3040 旧 gold 按章节标题重映射到 67 条新切片
> （144 条中 130 条有 gold），回归 **116 通过 / 0 跳过 / 0 失败**；同批 20 例跑 3 次，
> 严格答案级幻觉 70.6%–88.2%（均值 76.5%，标准差 10.2%），证明单轮不能当结论；
> 20 条独立盲评校准 MAE 0.059–0.110、refusal 一致率 1.0（Claude 复核，非人类标注）。
> 多轮产品主体改为结构化 `history_product`，覆盖纯中文产品名。详见
> [HANDOFF.md](HANDOFF.md) §3.106。
>
> 2026-11-19 图片内联：前端把召回图片切成 block，插到「图 N」引用行之后，打字机
> 随文字逐块展示；未改 proto/事件契约。真 token 级图文流留作后续需求，见
> [HANDOFF.md](HANDOFF.md) §3.107。
>
> 2026-11-19 章节路径：切分时维护 Markdown 标题层级栈，`section_path` 不再退化成
> 单个标题；`parent_title` / `chunk_key` 不变，现有集合需重新导入才看到新路径，
> 见 [HANDOFF.md](HANDOFF.md) §3.108。
>
> 2026-11-21 降幻觉：生成按问题类型路由（步骤类不补前置条件/排错分支、事实类逐字引用、
> 描述类只写证据/图注），judge 报告把 **core 幻觉** 置顶为主口径。同批 20 例 ×3 次：
> core claim 7.8%→**5.5%**，core 答案 40.7%→**26.7%**，严格答案级 76.5%→**68.6%**，
> claims/答案没有膨胀。详见 [HANDOFF.md](HANDOFF.md) §3.109。
>
> 2026-11-22 提示词统一：subagent 描述/系统提示、judge/answer/rewrite 的 system prompt
> 全部迁到根目录 `prompts/*.prompt`，代码统一 `load_prompt` 读取；10 份文本逐份比对一致，
> 见 [HANDOFF.md](HANDOFF.md) §3.110。
>
> 2026-11-22 目录统一：四个 subagent 都按 `agent/api/application/domain/infrastructure`
> 分层，配置统一放 `app/conf/`，spec 回到各自目录；旧路径与共享 tools 耦合已清理，
> 见 [HANDOFF.md](HANDOFF.md) §3.111 / §3.112。
>
> 2026-11-22 安全加固（批次 1）：上传文件名与撤回 `file_title` 只接受单段安全名，
> 删除不出 `output/`；HTTP 会话按 `user_id` 归属，管理端写接口要求 admin；
> MinIO 端口默认回环、Dockerfile 补 `scripts`。ItsMyGo 自己的 MinIO 也同步要求显式凭据、
> 端口默认回环（ItsMyGo `9cbb808`）。离线回归 **99 通过 / 27 跳过 / 0 失败**，
> 见 [HANDOFF.md](HANDOFF.md) §3.113。
>
> 2026-11-22 正确性批次：RAG 子图成本并入整轮账本（`mark/since` 差值保留子账），
> 终稿只从本轮新增消息取；下单预检/导购目录按 Java 实际上限 100；产品名未写入
> `kb_item_names` 时导入任务 failed；MinIO 图片前缀带尾斜杠并兼容历史前导斜杠；
> `kb_chunks` 改为先 upsert 后删陈旧 `chunk_key`；规格已识别时不再重复追问。
> 离线回归 **108 通过 / 27 跳过 / 0 失败**，见 [HANDOFF.md](HANDOFF.md) §3.114。
> token 级实时 delta 需跨端 reset 协议，另立需求。
>
> 2026-11-22 结构治理（批次 3）：`its_mygo_client` / `policy_repository` 上移到
> `app/clients`；order/policy 业务进入 application/domain，api 只做工具适配；
> `main_agent` 拆出 `stream_consumer` / `answer_reconcile` / `trace_labels`；
> Mongo/Redis/Neo4j/服务端口/政策/话术/认证等配置集中到 `app/conf`。真实依赖下回归
> **136 通过 / 0 跳过 / 0 失败**，结构规范见 [docs/structure.md](docs/structure.md)，
> 详见 [HANDOFF.md](HANDOFF.md) §3.115。
>
> 2026-11-22 测试基建（批次 4）：pytest 加入 dev 依赖与 CI，`tests/` 按
> unit / regression / integration 分层；新增 52 条 pytest（含 Mongo 会话归属、
> Redis 任务状态、Milvus 临时集合 upsert/delete 集成）；生产模块里已由 pytest 覆盖的
> 12 个 `_check_*` 删除，regression 精简为 122 条。Mongo 历史/去重客户端改为懒加载，
> 服务启动时显式预热。详见 [HANDOFF.md](HANDOFF.md) §3.116。
>
> 2026-11-22 清理：删除 `app/import_process` 旧启动 shim；`scripts/dev-joint.mjs` 与
> `.claude/launch.json` 统一走 `app.entrypoints.{import_http,agent_http,agent_grpc}`，
> 见 [HANDOFF.md](HANDOFF.md) §3.117。

历史里程碑与具体日期见 [HANDOFF.md](HANDOFF.md)；需求与范围见 [docs/requirements.md](docs/requirements.md)，
技术选型见 [docs/architecture.md](docs/architecture.md)。

P3 LLM-as-Judge 用法：`python -m app.eval.judge check` / `run --limit 3`，
结果写到 `output/eval/`；管理端从平台 `/admin/agent-eval` 进入（本仓库不提供页面）。
管理端还可以新建自建用例（保存到 `output/eval/cases_custom.yaml`，与内置用例合并）。

## 文档

| 文件 | 作用 |
|---|---|
| [docs/requirements.md](docs/requirements.md) | **需求分析** —— 产品定位、场景、功能/非功能需求、平台对接前提、验收标准、范围外、里程碑 |
| [docs/architecture.md](docs/architecture.md) | **架构选型** —— 技术栈选型与理由、总体架构、模块划分、契约概览、关键取舍、决策记录 D1–D26 |
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
