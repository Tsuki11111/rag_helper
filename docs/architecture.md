# 架构选型 · 买小购（一次买够平台客服智能体）

> **本文定位**：回答「**选了什么、为什么选它、整体长什么样**」。
> 「**具体怎么实现、代价是什么**」在 [implementation.md](../implementation.md)（技术详细实现），
> 「**当前状态 / 坑 / 操作**」以 [HANDOFF.md](../HANDOFF.md) 为准，
> 「**要做什么、做到什么程度**」在 [docs/requirements.md](requirements.md)。
> 接口契约的**源码**是 [proto/agent.proto](../proto/agent.proto)；部署操作见 [docs/deployment.md](deployment.md)；
> 评估方案见 [evaluation-plan.md](../evaluation-plan.md)。
>
> 归并说明：原 `evolution-plan.md` 的**需求与路线**部分并入 `docs/requirements.md`，
> **技术依据与契约**部分并入本文；原 `docs/decisions-evolution.md` 的 D1–D8 并入本文 §6。

---

## 0. 文档定位与权威关系

| 问题 | 看哪里 |
|---|---|
| 要做什么 / 验收标准 / 范围外 | [docs/requirements.md](requirements.md) |
| 选了什么技术、为什么、整体架构 | **本文** |
| 每个节点/每层具体怎么实现 | [implementation.md](../implementation.md) |
| 现在做到哪、有哪些坑、怎么操作 | [HANDOFF.md](../HANDOFF.md) |
| gRPC 契约的字段与语义 | [proto/agent.proto](../proto/agent.proto)（源码即权威） |
| 怎么部署、端口、联合启动 | [docs/deployment.md](deployment.md) |
| 质量怎么评估 | [evaluation-plan.md](../evaluation-plan.md) |

**冲突时的优先级**：契约以 `proto/agent.proto` 为准；状态与坑以 HANDOFF 为准；需求与范围以
requirements 为准；本文与 implementation 冲突时以实现侧（implementation）为准并回头修本文。

---

## 1. 系统总览

### 1.1 形态：两条图 + 一层 agent

```
导入图（LangGraph，8 节点）：PDF/MD → 解析 → 读图 → 切分 → 产品主体识别 → 向量化 → 入 Milvus（+可选 Neo4j）

询问入口（HTTP/SSE + gRPC，同源）
  └─ 主 agent（deepagents，唯一对用户说话的「嘴」）
       ├─ subagent product-kb    ← 检索图（LangGraph，10 节点 / 四路召回 / RRF / 重排 / 有界循环）
       ├─ subagent product-guide ← 商品目录：关键词 / 分类 / 价格 / 语义检索
       └─ subagent order-service ← 订单 / 物流 / 地址 / 下单·退款·催办草稿
```

- **主 agent 是唯一「嘴」**：用户可见的问题与澄清都由主 agent 出口；subagent 只干活、只回结果。
  这是「RAG 无状态化」的根据，也是防「两张嘴打架」的红线（D3）。
- **检索图是固定 DAG**，唯一有循环的地方是 `node_rerank` 之后的有界自纠正（见 §5、D8）。
- **两种 subagent 形态并存**：检索图用 `CompiledSubAgent`（已编译的图），
  订单/导购用 deepagents 的声明式 `SubAgent`（tools + system_prompt）。

### 1.2 三条边界与部署形态

1. **浏览器只跟 Java 说话**——agent 服务只在内网、只允许平台单体访问；
2. **agent 只认两个东西**：服务端密钥（平台/商户）+ `X-User-Id`（顾客）；
3. **上传接口不接受 `X-User-Id`**——管理端行为，走 HTTP + 访问密钥那一档。

部署形态：Java 单体（ItsMyGo）+ **agent 单实例**（不是微服务、不经网关）。
**唯一支持的启动形态是与平台联合部署**（`./deploy.sh joint`）：本仓库已移除单栈启动
与全部自用页面（2026-11-06），用户界面与管理界面都由平台侧提供。
生产路径走 gRPC（`Converse` 事件流）；管理端（导入 / 评测）走 HTTP，由 Java 代理。
「单实例」仍是有意选择：SSE 队列与任务状态跨进程的债尚未还清（见 §7）。

---

## 2. 技术栈与选型理由

| 层 | 选型 | 为什么选它 | 被否掉的方案 / 代价 |
|---|---|---|---|
| 编排 | 主 agent：**deepagents**（LangGraph 之上）；检索图：**LangGraph** | subagent 机制是「同步调用、上下文隔离、只回最终结果」，与「RAG 作为能力被编排」天然吻合；与现有 langchain/langgraph 栈同源，检索图/检查点/记账/护栏全部复用 | 不用「RAG 当工具」的旧论证：deepagents 的 subagent 本质就是工具形态的 agent 图，仍只有一份对话状态 |
| 主 agent 大模型 | **DeepSeek 官方 API**（`deepseek-flash`，直连） | tool calling 实测 7/7 全派发；qwen-plus 同提示词下 4/6 会跳过检索 | qwen-max 被否决（成本/用户口径）；代价是多一套供应商与密钥 |
| 检索链路大模型 | 通义千问（OpenAI 兼容端点，`langchain-openai`） | 与嵌入/重排同供应商，链路短；OpenAI 兼容免自建适配 | 需注意 `stream_usage` 不显式打开会静默漏记 token |
| 嵌入 | DashScope `text-embedding-v2`（1536 维，仅稠密） | 只需稠密向量；四路召回靠 RRF 融合，不依赖稀疏向量 | 不引入 BM25/稀疏向量，检索召回靠多路兜住 |
| 重排 | DashScope `gte-rerank-v2`（原生端点） | 托管精排，免部署本地重排模型；动态 Top-K 按 API 的 0~1 分数尺度调过 | 不用本地 BGE；代价是重排是最贵的一项（占单次成本约 43%） |
| 向量库 | **Milvus 2.5**（standalone） | 支持向量 + 标量过滤混合检索（`item_name in [...]`），切片级过滤是一等需求 | 不用 pgvector/纯内存：数据规模与过滤表达力 |
| 对象存储 | **MinIO** | 自建 S3 兼容，图片与原始文件同源；桶名不允许下划线（`rag-helper-*`） | — |
| 会话/去重/账本/检查点 | **MongoDB** | 文档型适合会话与运行记录；TTL 索引回收检查点（7 天）；`MongoDBSaver` 是 LangGraph 官方后端 | Milvus 不能存无向量集合（实测报错），所以去重记录放 Mongo |
| 任务状态/暂停标志 | **Redis 7**（appendonly） | 跨进程可读、重启不丢；停止标志必须跨 HTTP 请求可见 | 降级为进程内存（观感问题可接受，停止标志双写+并集读） |
| 知识图谱 | **Neo4j 5.26**（**可选，默认关闭**） | 图谱那一路是召回之一，不是主路径；检索循环/RRF 已能兜住 | 默认 `NEO4J_ENABLED=false`：内存占用大，关闭时是**配置跳过**不是降级故障 |
| 图谱可视化 | ECharts 5.4.3（CDN，三源回退） | 管理端看图 | — |
| PDF 解析 | **MinerU**（云端 API） | 版式/表格/图片抽取交给托管服务，避免本地跑重模型 | 按页数配额计费，不在 token 账本内；重导会重烧配额 |
| 联网搜索 | 百炼 MCP `EnhancedSearch`（`openai-agents` 客户端，**`mcp<2`**） | 官方托管检索；`mcp` 2.x 改了握手协议，百炼服务端仍是旧协议会回 500，故显式钉版本 | 升级 `mcp` 会让联网搜索全挂（已知坑） |
| 对外接入 | **gRPC**（对 Java）+ **HTTP API**（管理端导入 / 评测，Java 代理） | gRPC 让「流的归属 = 这次调用」，天然不存在订阅到别人会话的问题；事件流用 protobuf oneof 表达；管理端沿用 HTTP + 访问密钥 | 不用 REST/SSE 作为生产问答路径（HTTP/SSE 接口仅保留作调试）；SSE 队列的单进程限制因此不阻塞生产 |
| Web | FastAPI（**只提供 HTTP API，无前端页面**） | 本仓库已移除自用页面（2026-11-06），界面全在平台侧；服务端不引前端工程化 | 不用 FastAPI 托管页面/静态资源 |
| 包管理 | uv（Python ≥ 3.12） | lock 文件可复现；`.venv` 里没有 pip 是 uv 的正常行为 | 不要用 pip 往 `.venv` 装包（会让 lock 与实际脱钩） |

**选型总原则**：能用托管 API 就不自建模型；能复用既有存储就不引新组件；每个可选组件
（Neo4j、图谱、检索循环）都必须能**配置关闭**且关闭后行为有明确语义。

---

## 3. 总体架构

### 3.1 组件与端口

| 服务 | 容器 | 宿主机端口 | 说明 |
|---|---|---|---|
| 知识库导入 | `rag-helper-agent-import` | 8001 | 管理端 HTTP（由 Java 代理）；PDF/MD 上传与导入图，无页面 |
| HTTP / SSE | `rag-helper-agent-http` | 8002 | 管理端 HTTP（评测接口，由 Java 代理）+ 会话/对话调试接口；无页面 |
| gRPC | `rag-helper-agent-grpc` | 8003 | 平台调用入口（`Converse` / `Stop` / `ListSessions` / `GetHistory`） |
| MinIO | `rag-helper-minio` | 9002 / 9003 | 图片与原始文件 / 控制台 |
| Milvus | `rag-helper-milvus` | 19530 / 9091 | 向量库 / metrics |
| MongoDB | `rag-helper-mongo` | 27018 | 会话、去重、账本、运行记录、检查点 |
| Redis | `rag-helper-redis` | 6380 | 任务进度、停止标志 |

端口全部可用 `.env` 的 `RAG_*_PORT` 覆盖；默认值刻意避开 ItsMyGo 与本机共用容器。
中间件带 `rag-` 唯一别名，避免联合部署时与 ItsMyGo 的 `minio` / `redis` 服务名冲突。

### 3.2 一轮询问的数据流

```
Java ──gRPC Converse(message, session_id)──> agent 服务
  1. 鉴权：metadata 的 authorization + x-user-id（+ 每轮签发的 x-agent-tool-ticket）
  2. 输入护栏：不合规直接拒（不烧模型调用）
  3. usage_context：trace_id = run_id，整轮墙钟预算开始计时
  4. 读会话历史（chat_message，按 user_id 过滤）→ 组装主 agent 入参
  5. 主 agent（deepagents）model↔tools 循环：
       ├─ 决策派发哪个 subagent
       ├─ subagent 干活（检索图 / 商品目录 / 订单工具），上下文隔离、只回结果
       └─ 工具经「票据」回调 Java 内部 HTTP 接口
  6. 事件流：SessionStart → Trace* → Delta* → Usage* → Final（含配图 / 商品卡）
  7. 收尾：写 chat_message、写 query_runs、汇总本轮成本
```

**「业务函数同源」**：HTTP `/query` 与 gRPC `Converse` 复用同一个 `run_main_agent`——
新能力只加 subagent，不自建 API/服务/端口。

### 3.3 模块划分与职责

| 模块 | 职责 | 详细实现 |
|---|---|---|
| `app/import_process/` | 导入图：解析、切图、切分、产品主体识别、向量化、图谱导入、去重与续跑 | [implementation.md](../implementation.md) §3 |
| `app/agent/sub_agents/product_helper_agent/` | 检索图：产品确认、四路召回、RRF、重排、有界循环、答案生成与配图引用 | 同上 §4 |
| `app/agent/main_agent.py` | 主 agent、`run_main_agent` 一轮生命周期、subagent 注册、事件流映射 | 同上 §5 |
| `app/agent/sub_agents/order_service_agent/` | 订单查询、物流摘要、地址、下单/退款/催办草稿与执行图 | 同上 §5 + HANDOFF §3.58–§3.65 |
| `app/agent/sub_agents/product_guide_agent/` | 商品目录检索、语义检索、商品卡收集与裁剪 | 同上 §5 |
| `app/agent/api/` | HTTP/SSE、gRPC 服务端、鉴权、会话、停止、评测接口 | 同上 §6 |
| `app/clients/` | Milvus / Mongo / Redis / MinIO / Neo4j / ItsMyGo 回调 | 同上 §7 |
| `app/core/` | 记账、预算、重试、日志、护栏、任务状态、回归套件、评测 | 同上 §8、§10 |
| `proto/agent.proto` | 对外 gRPC 契约（**双方唯一契约**） | §4 |

---

## 4. 接口与契约概览

> **Java 侧对应文档**：ItsMyGo 的 `docs/architecture.md` §4（接口与契约概览）/ §9（跨仓库协作）、
> `docs/api.md` §19（客服智能体「买小购」接口）。字段权威仍是本仓库的
> [proto/agent.proto](../proto/agent.proto)；对端只是镜像并生成 stub。

### 4.1 gRPC（`proto/agent.proto`）

四个 RPC：`Converse`（流式事件）、`Stop`、`ListSessions`、`GetHistory`。

`Event` 是 oneof，共 6 类：`start`（session_id + run_id）、`trace`（统一执行流）、
`delta`（流式增量）、`usage`（累计用量）、`final`（完整回复 + 配图 + 商品卡）、`error`。

`Trace.kind` 取值：`delegate`（派发 subagent）/ `tool`（调工具）/ `result`（工具返回摘要）/
`reply`（生成回复）/ `node`（subagent 图内节点步骤）。UI 一律渲染成灰字执行流。

**与旧草案的两处实质差异**：`Confirm` / `Resume` 消失（产品确认由主 agent 对话承接，
每轮都是新 `Converse`）；`Progress` 升级为 `Trace`（主 agent 与 subagent 同一种事件）。

**契约铁律**：proto 是双方唯一契约，本仓库是源头；**只加字段不删改**（老消息返回空列表）；
改 proto 必须双端同步（Java 侧 ItsMyGo 直接生成 stub）。`run_id` 由本服务生成，
平台存映射，**不透传当 thread_id**（撞 id = 共用检查点）。

### 4.2 agent → Java 内部 HTTP 工具回调

| 方向 | 接口 | 用途 |
|---|---|---|
| agent → Java | `POST /internal/agent/orders/query` | 查当前用户订单（含物流摘要，无 PII） |
| agent → Java | `POST /internal/agent/catalog/products`、`GET /internal/agent/catalog/all|categories` | 商品目录 / 分类 |
| agent → Java | `POST /internal/agent/actions/order|refund|urge-shipment|urge-refund`（及对应 `/send`） | 准备草稿 / 执行发送 |

**身份**：Java 每次 `Converse` 签发短期票据（Redis，10 分钟，须 ≥ gRPC deadline 240s），
经 metadata `x-agent-tool-ticket` 下发；工具回调带回 `X-Agent-Ticket`。
**模型不参与身份**——Java 只认票据里的 `userId`。

### 4.3 确认卡片与内部事件流程

```
agent 工具 prepare（只生成草稿，不执行） → Java 落 agent_actions（PENDING_CONFIRM 快照）
  → 用户在 Java 聊天卡片确认 → Java 按服务端快照执行（参数全部来自快照，agent 无法指定用户/金额）
  → Java 触发内部事件（urge-shipment-confirmed / urge-refund-confirmed / order-confirmed）
  → agent 执行图：调 Java 发送工具（或直接生成回复）→ 写一条 assistant 历史
```

**红线**：工具返回文本必须写明「尚未下单 / 尚未提交 / 草稿已生成」；模型不许声称已执行。

### 4.4 管理端 HTTP 接口（供平台代理，本仓库无页面）

| 域 | 端口 | 接口 | 平台侧代理 |
|---|---|---|---|
| 知识库导入 | 8001 | `POST /upload`、`GET /documents`、`GET /documents/graph`、`GET /status/{task_id}`、`DELETE /documents/{file_title}` | `/admin/knowledge` |
| 评测 | 8002 | `/eval/cases`、`/eval/run`、`/eval/results`、`/eval/jobs`、`/eval/jobs/{id}/stop` | `/admin/agent-eval` |
| **知识缺口（2026-11-06 新增）** | 8001 | `GET /knowledge-gaps`、`GET /knowledge-gaps/report`、`POST /knowledge-gaps/draft`、`POST /knowledge-gaps/publish`、`POST /knowledge-gaps/status` | 已接入（ItsMyGo `68b8960`，页面 `/admin/knowledge-gaps`） |

全部走访问密钥（`Depends(current_tenant)`），不属于 gRPC 契约；新增这类接口不改 `proto/agent.proto`。

---

## 5. 关键设计取舍汇总

| 决策 | 取舍 | 代价 / 边界 |
|---|---|---|
| 主 agent 是唯一「嘴」，subagent 只干活 | 避免两张嘴打架；RAG 可无状态化 | 澄清能力集中在一个提示词，qwen 类模型对软措辞不买账，必须写到「必须遵守」级别 |
| 统一询问入口（HTTP 与 gRPC 同源） | 新能力零新增对外接口 | 所有能力都受主 agent 编排质量影响，编排坏则全部坏 |
| RAG 图内 HITL 退役，改为返回候选 | 主 agent 对话承接确认 | 查询侧确认卡片相关能力整体退场，导入侧检查点照旧 |
| 检索图内加有界自纠正循环 | 修复「一次检索定生死」 | 尾延迟与成本上升，靠轮次/预算/守卫三重上限兜住；默认关闭 |
| 写操作只 prepare + 用户确认 + Java 执行 | 资损风险不在模型手里 | 多一套 `agent_actions` 状态机与内部事件通道 |
| 单个实例 | 大幅简化分布式问题 | SSE 队列与任务状态尚不支持多 worker；扩容前要还债 |
| 知识库全平台共享一份 | 不重建 Milvus/Neo4j/MinIO | 隔离对象只剩「人的痕迹」：会话、账本、运行记录 |
| 可选组件统一可配置关闭 | 环境轻量、故障可控 | 「配置跳过」与「降级故障」必须语义上分清（`degraded` 标记） |
| 产品名目录（`kb_item_names`）「先来先占」 | 同名产品多文档时不互相顶占；撤回按 `kb_chunks` 反查重建 | 主键仍是 `item_name`，目录里只保留先来文档；多文档并存需改主键（停机迁移） |

完整设计取舍与逐项理由见 [implementation.md](../implementation.md) §11。

---

## 6. 决策记录（D1–D8）

> 原 `docs/decisions-evolution.md` 已并入本节。现状备注截至 2026-11-05。

| # | 决策 | 理由 | 现状 |
|---|---|---|---|
| D1 | 目标形态：deepagents 主 agent + RAG 降为 subagent | subagent 机制（同步、上下文隔离、只回结果）与「RAG 作为能力被编排」吻合；与现有 langchain/langgraph 栈同源，资产全复用 | 已落地（M0–M13） |
| D2 | 一个月窗口验收 = C 档（gRPC 服务化 + RAG 闭环 + 只读订单 mock） | proto 冻结后 Java 侧才能并行；真联调节奏不在自己手里 | 已超额完成：2026-10-25 完成联合部署与真实联调 |
| D3 | RAG 无状态化：中断收归上层、历史归主 agent | 子 agent 中途提问会与主 agent「两张嘴打架」；「永远只有一份对话状态」是旧红线在新框架的落地 | 已落地；查询侧 HITL 退役，导入侧检查点保留 |
| D4 | 删除 `java-integration.md`，资产搬入路线文档 | 避免两份路线文档并行演化、各自称权威 | 本文即该资产的最终归并位置 |
| D5 | 企业化与评估两条路线收编、随新形态重置 | 对象变为「主 agent 编排质量 + subagent 质量」，按旧对象排期等于为不存在的主体重投资 | 企业化 8/16，评估 P0/P1/P3 完成 |
| D6 | 界面：统一执行流（`Trace` 事件） | 客服场景看对话本身；完整界面由 Java 实现，事件契约承载渲染所需信息 | 曾落地（M5）；**2026-11-06 本仓库页面已删除**，界面完全由平台侧实现，`Trace` 契约不变 |
| D7 | 演示语料：现有手册 + 1–2 份 FAQ | 演示价值在多 subagent 协作；真实客服语料在平台侧拿不到 | 保持现状（未新增 FAQ） |
| D8 | 检索循环加在子图内部，不在主 agent 层再套一层 | 主 agent 已是 model↔tools 循环，再套会与事件流/预算/暂停/运行记录打架；**对抗目标漂移靠「意图锚 + 验证」而不是循环次数** | 已落地（M6），默认关闭，待 gold 对比定默认值 |

**三条业务红线**（做任何业务 subagent 时都成立）：
1. LLM / RAG **永远不直接写业务库**——只能「提议动作」，由 Java 校验并执行；
2. **风控决策不用 LLM**——模型出信号、规则引擎判定，否则资损无法解释、无法复现；
3. **高危动作必须人工审批 + 全程审计**——客服场景从加分项变刚需。

---

## 7. 已知限制与技术债

只列架构层面的；完整清单以 [HANDOFF.md](../HANDOFF.md) §8.11 为准。

| 限制 | 影响 | 备注 |
|---|---|---|
| 单实例、SSE 队列在进程内 | 不能开多 worker；多实例需 sticky | 生产路径走 gRPC，不受影响 |
| 写操作没有 append-only 审计 | 出问题无法完整回溯 | 见 requirements §3.8 / 后续优先级 |
| 间接注入（知识库/联网内容）未稳定验证 | 供应链内容可能携带指令 | 直接注入已复测 17 条 |
| 数据隔离只做到会话与账本 | 成本归因、跨租户 A/B 验收未完成 | 见 requirements §3.8 |
| 商品语义索引在内存（TTL 300s） | 商品量大后内存与刷新成本上升 | 到时迁 Milvus |
| 图谱排序只是近似 | df 反映抽取覆盖度而非真实词频 | 精度由下游重排兜住 |
| 图片 URL 默认 `127.0.0.1` | 浏览器不在本机时图裂 | 内网部署需改 `RAG_MINIO_PUBLIC_ENDPOINT` 并重写存量 URL |

---

## 8. 与详细实现文档的关系

- **本文**：选型、架构、边界、决策——**读者是评审者、新加入者、要判断「为什么」的人**；
- **[implementation.md](../implementation.md)**：每个节点的算法、状态字段、数据表结构、错误处理、
  命令与文件地图——**读者是改代码的人**；
- 两者**允许重复观点**，但**细节只在 implementation**；本文若与 implementation 冲突，以实现为准并回头修本文。
