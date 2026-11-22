# 架构选型 · 买小购（一次买够平台客服智能体）

> **本文定位**：回答「**选了什么、为什么选它、整体长什么样**」。
> 「**具体怎么实现、代价是什么**」在 [implementation.md](../implementation.md)（技术详细实现），
> 「**当前状态 / 坑 / 操作**」以 [HANDOFF.md](../HANDOFF.md) 为准，
> 「**要做什么、做到什么程度**」在 [docs/requirements.md](requirements.md)。
> 接口契约的**源码**是 [proto/agent.proto](../proto/agent.proto)；部署操作见 [docs/deployment.md](deployment.md)；
> 评估方案见 [evaluation-plan.md](../evaluation-plan.md)。
>
> 归并说明：原 `evolution-plan.md` 的**需求与路线**部分并入 `docs/requirements.md`，
> **技术依据与契约**部分并入本文；原 `docs/decisions-evolution.md` 的 D1–D8 与后续 D9–D15 并入本文 §6。

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
导入图（LangGraph，8 节点）：PDF/MD → 解析 → 读图 → 切分 → 产品主体识别 → 向量化（dense + sparse）→ 入 Milvus（+可选 Neo4j）

询问入口（HTTP/SSE + gRPC，同源）
  └─ 主 agent（deepagents，唯一对用户说话的「嘴」）
       ├─ subagent product-kb    ← 检索图（LangGraph，五路召回 / RRF / 重排 / 有界循环）
       ├─ subagent product-guide ← 商品目录：关键词 / 分类 / 价格 / 语义检索
       ├─ subagent order-service ← 订单 / 物流 / 地址 / 下单·退款·催办草稿
       └─ subagent policy-agent ← 平台政策 / 售后规则 / 客服话术
```

- **主 agent 是唯一「嘴」**：用户可见的问题与澄清都由主 agent 出口；subagent 只干活、只回结果。
  这是「RAG 无状态化」的根据，也是防「两张嘴打架」的红线（D3）。
- **检索图是固定 DAG**，唯一有循环的地方是 `node_rerank` 之后的有界自纠正（见 §5、D8）。
- **两种 subagent 形态并存**：检索图用 `CompiledSubAgent`（已编译的图），
  订单/导购/政策用 deepagents 的声明式 `SubAgent`（tools + system_prompt）。

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
| 嵌入 | DashScope `qwen3.7-text-embedding`（1536 维，dense + sparse） | 稠密负责语义、稀疏负责型号/故障码/参数名等词面命中；一个模型同时产出两种向量 | 代价是原生端点与稀疏索引；仍保留 dense 回退，不让 sparse 成为单点；切片层另加低权重应用层 BM25 补章节词 |
| 重排 | DashScope `qwen3.7-text-rerank`（原生端点） | 托管精排，免部署本地重排模型；动态 Top-K 按 API 的 0~1 分数尺度调过 | 不用本地 BGE；代价是重排是最贵的一项（占单次成本约 43%） |
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
| `app/importing/` | 导入图：解析、切图、切分、产品主体识别、向量化、图谱导入、去重与续跑 | [implementation.md](../implementation.md) §3 |
| `app/agent/subagents/product_kb/` | 检索图：产品确认、五路召回、RRF、重排、有界循环、答案生成与配图引用 | 同上 §4 |
| `app/agent/runtime/main_agent.py` | 主 agent、`run_main_agent` 一轮生命周期、subagent 注册、事件流映射 | 同上 §5 |
| `app/agent/subagents/order_service/` | 订单查询、物流摘要、地址、下单/退款/催办草稿与执行图 | 同上 §5 + HANDOFF §3.58–§3.65 |
| `app/agent/subagents/product_guide/` | 商品目录检索、语义检索、商品卡收集与裁剪 | 同上 §5 |
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
`reply`（生成回复）/ `node`（subagent 图内节点步骤）/ `form`（表单回传处理）。
UI 一律渲染成灰字执行流。**`Trace` 只承载用户可读动作**：`agent` 为执行主体名字
（买小购 / 产品知识库智能体 / 商品导购智能体 / 订单服务智能体），`detail` 为中文动作（如「查询在售商品」
「已完成」），不暴露工具名、调用参数或工具返回原文。

**客服设置与流式（2026-11-07）**：`ConverseRequest` 新增两个 **optional** 字段
`enable_web_search` / `stream`（未设置时联网=开、流式=关，保持旧行为）；`stream=true` 时
服务端下发**最终回答**的增量 `Event.delta`（`Delta{delta}`；中间工具轮次文本不回放），前端把增量追加到本轮回复上，
最后以 `FinalEvent.answer` 整段替换（防重复）。`FinalEvent.forms` 下发可交互表单
（`FormRequest{form_id, form_type, title, description, fields, submit_label,
expires_in_seconds, meta}`），`ConverseRequest.form_result{form_id, values}` 回传；
`HistoryMessage.forms` 只读回放。服务端规则：**一轮最多 1 张**、`form_id` 一次性消费、
TTL 600s、按 `form_type` **直接调工具**（不经过模型猜参数）；`meta` 是 agent 侧上下文
（前端不渲染，例如 `{"orderNo": …}`）。首批只做 `refund_reason`；旧客户端不传
`form_result` 时按普通文本对话，旧 Java 忽略 `forms` 即可兼容。

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

读接口走访问密钥（`Depends(current_tenant)`），写接口（上传/撤回/知识缺口写操作/评测写操作）
走 `Depends(require_admin)`；不属于 gRPC 契约，新增这类接口不改 `proto/agent.proto`。

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
| 工具失败三分（未授权 / 暂时故障 / 业务拒绝）+ 只读重试、写操作永不重试 | 暂时故障靠重试自愈；写操作重试会生成重复草稿/消息 | 写操作失败要用户重来；重试只覆盖只读工具（查订单/目录/地址） |
| 动作前预检（SKU / 库存 / 地址 / 订单状态） | 提前拦住必然被 Java 拒绝的动作，减少无效确认卡片 | 预检是 best-effort：目录接口一次最多 200 条，超过上限时跳过缺失商品校验 |

完整设计取舍与逐项理由见 [implementation.md](../implementation.md) §11。

---

## 6. 决策记录（D1–D37）

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
| D9 | 人格与话术集中到 `prompts/tone_guide.prompt`，注入主 agent 与 `answer_out` | 两处「说话」各写一套风格必然漂移；单一来源便于统一验收与修改 | 2026-11-07 落地；回归守注入（唯一标记） |
| D10 | 工具失败三分（未授权/暂时故障/业务拒绝）：只读有限重试、写操作绝不重试 | 暂时性故障可自愈，但写操作重试会产生重复草稿/重复消息（资损与体验风险） | 2026-11-07 落地；回归守策略（含写操作超时只调 1 次） |
| D12 | 客服设置用 gRPC `optional` 字段表达开关（未设置=旧行为），偏好存 Java 表 | 只需二进制兼容即可灰度：旧 Java 不传、旧 agent 不认都不破坏现有链路 | 2026-11-07；流式为真增量（`Delta`），关时不推 | 
| D13 | 智能推广按「降价 > 相似购买/画像 > 销量 > 评价」组合打分，登录后随机延迟 + 客服会话卡片推送 | 降价是最强购买动机，但需要价格历史；没有历史的商品自然拿不到降价分（不会编造）。2026-11-12 起接入用户画像：非冷启动时降价 0.35 / 近期相似 0.20 / 画像 0.20 / 销量 0.15 / 评分 0.10，冷启动回落旧权重 | 2026-11-07 落地；2026-11-12 接画像，价格历史从上线起积累 |
| D15 | 客服话术用**独立 FAQ 集合 + 单路稠密检索**，主 agent 直调 | 话术回答的是「怎么说」而不是知识事实；混进 `kb_chunks` 会被产品问答召回污染，单路检索足够且延迟低，不引入重排/多路 | 2026-11-08；`kb_tone_faq` + `search_tone_faq`，阈值 0.30、Top3 |
| D14 | 消费报告由 Java 聚合（不调模型），agent 只做摘要 | 金额/排名必须可核对，模型只负责组织语言，避免算错钱 | 2026-11-07；agent 工具 `get_consumption_report` 调内部只读接口 |
| D11 | 会话内表单：`forms` 走结构化事件通道，回传走 `ConverseRequest.form_result`（方案 A） | 表单无法可靠塞进文本；回传复用 Converse 的票据/限流/审计；服务端按 form_type 直接调工具，避免模型猜参数 | 2026-11-07 评审通过并双端落地；首批 `refund_reason` |
| D16 | 平台政策用「MySQL 章节原文为准 + Milvus 章节向量召回」的混合方案 | 政策回答必须可引用、可版本化，纯向量会漏条款、混版本；纯 DB 又接不住口语化提问。章节入库保留原文与结构，向量只负责召回，LLM 不直接解释未命中的条款 | 2026-11-10 开工；`platform_policy` / `platform_policy_section` + `kb_platform_policy` |
| D17 | 用户画像由 Java 实时聚合 + Redis 缓存，作为导购工具与推广打分共用信号 | 画像事实（品类/品牌/价格带）必须来自真实订单与足迹，不能靠 LLM 编；实时聚合避免维护画像表与埋点；缓存 30 分钟兼顾新鲜度与查询成本；导购工具只拿聚合结论，不暴露订单号/地址/聊天原文 | 2026-11-12；`GET /internal/agent/profile` + `get_user_profile`（product-guide）+ 推广画像权重 |
| D18 | 嵌入升级 `qwen3.7-text-embedding`（1536，dense + sparse）；`kb_chunks` 混合检索，其他集合仍 dense-only | 评测 hit 94.1% 但 MRR 仅 0.578，精确型号/故障码常被语义召回稀释；稀疏向量补词面命中，dense 保语义与降级。只在切片集合引入 sparse，产品名/政策/话术仍单路稠密，避免为统一而增加无收益的复杂度 | 2026-11-15 落地；旧数据全量清空重建，导入/检索/回退均有回归。2026-11-17 集合迁到 `kb_chunks_v2`（见 D22），dense+sparse 混合检索构成不变 |
| D19 | 检索加第五路**应用层 BM25**，作为 `node_rrf` 的一路按权重融合（默认 0.2），不单独成第三层等权 RRF | 语义召回对型号/章节词不敏感，sparse 又容易把 `目录`/`重要事项` 顶上来；BM25 补精确词面，但只做低权重一路，避免重复放大词法偏差。610 条切片内存索引，300 秒缓存，不改 Milvus schema、不重新 embedding | 2026-11-15 落地；BM25 24 条 MRR 0.3843→0.5245，但产品名识别仍由 LLM 从目录猜，需 D20 |
| D20 | **认产品与找证据分层**：产品名/别名/型号/部件走产品目录 BM25 + 规则实体链接；证据检索走 dense/HyDE，切片 BM25 只做低权重补充 | `step_3` 从标准名 catalog 里选产品，用户没提产品时也会硬凑；同一零上下文问题两次运行一次认出 HAK180、一次认不出。实体链接是低频强区分度匹配，适合 BM25 + 别名/型号/部件词典；长文档证据适合语义检索。产品目录单列 `product_catalog.json`，可审计、可回归 | 2026-11-16 落地；`step_3` 只改写，`resolve_product` 返回 confirmed/candidates/none；缺规格先问全幅/半幅。2026-11-17 补**同手册多型号归一**：catalog 用 `manual_id` + `model_names`，HL-3040CN/HL-3070CW 共用一本手册，回答默认通用、只按证据说差异 |
| D21 | **敏感主题在入口短路为 `rejected`**，不派发 RAG、不调模型 | 子图 `judge_outcome` 只能看到子图 answer；主 agent 的最终拒答发生在子图之后，导致“实际安全拒答、统计仍 answered”。把合规拒答前移到主 agent 入口，既消除模型是否礼貌拒答的抖动，也让 `rejected` 语义确定；与输入注入的 `blocked` 分开 | 2026-11-16 落地；`app/core/safety_guard.py` + 主 agent 入口分支；`sensitive_tiananmen` 不再进 RAG |
| D22 | **切片集合换 `chunk_key` 稳定业务键 + 章节/版本身份**（`kb_chunks_v2`），不再用自增 `chunk_id` 当唯一身份 | 自增主键让每次重导的 `chunk_id` 全变：评测 gold 全部断链、无法按内容 upsert、增量更新只能整文档重灌；只有 `title`/`parent_title`/`part` 三个字符串/序号，同名标题会串章节、迁移后也无法追溯回旧切片。稳定键取 `doc_id + section_id + 章节内 ordinal`，**不含正文**：正文修订 `chunk_key` 不变、`content_hash` 变，新增/删切片才动序号 | 2026-11-17 落地；`app/utils/chunk_identity.py` + `scripts/migrate_chunks_v2.py` 迁移 893 条，`chunk_key`/`chunk_id` 唯一、`sparse_vector` 全非空，`.env` 切 `CHUNKS_COLLECTION=kb_chunks_v2`；**不改原表 ALTER**；图谱边仍用 `chunk_id` 兼容，迁移数据的 `chunk_id` 保留旧值。当天晚些集合名归一为 `kb_chunks`（D26） |
| D23 | **受控补充建议**：手册没写时允许单独一段通用建议（固定开头 + 1–2 句），judge 侧用 `claim_type=advisory` 与 `unsupported` 分开统计，幻觉率不含 advisory | 一刀切禁止建议会把「手册没写」变成生硬拒答；完全放开则「说得像建议」能把编造洗成正常。做法是**话术上强约束边界 + 指标上分开统计**：固定开头让用户一眼看出不是手册内容，同时 advisory 必须有 `hedged`/`hedge_text` 证据，引用越界或 contradictory 时强制降级为 unsupported，所以免责声明不能豁免具体型号/参数结论 | 2026-11-17 落地；`answer_out.prompt` 第 5 条 + `judge_groundedness.prompt` 逐条 claim 标注；报告新增 `hallucination_claim_rate` / `hallucination_answer_rate` / `advisory_claim_rate` / `advisory_answer_rate` / `evidence_coverage_rate`，回归用例「judge 补充建议与幻觉统计口径」 |
| D24 | **生成上下文的切片头部标注出处**（`手册名 · 章节路径`），内部标识字段不进上下文 | 多手册并存时模型只看章节标题分不清来源，容易把 A 机型手册的操作写到 B 机型；`chunk_key`/`content_hash`/`doc_version`/`chunk_index` 只服务内部引用与过滤，喂给模型等于稀释注意力。**页码暂不展示**：`page_no` 目前 893 条全为 0（MinerU→md→切分不产页码），展示恒为 0 的页码比不展示更糟，等解析侧补齐后再议 | 2026-11-17 落地；`_build_context` 头部 `[i] 手册名 · 章节路径`，`section_path` 与标题相同时不重复，头部截断 80 字符；回归用例「上下文标注切片出处」 |
| D25 | **切片 schema 只留被读的 12 个字段**，重建集合（不新增 v3）；身份公式与旧 `chunk_id` 复用不变，中间量（`doc_id`/`section_id`/`content_hash`）不再落盘 | 实测 24 个字段里只有 9 个在运行时被读，15 个只写不读；schema 宽度**不消耗**上下文（进 prompt 的只有 `output_fields` 点名的字段），所以精简的依据是「有没有读方」而不是省上下文。保留列都对应一个真实读方或稳定键根：`chunk_key`（身份根）、`chunk_id`（全部链路与 130 条 gold 的 join key）、`file_title`/`title`/`parent_title`/`section_path`（回查过滤与出处标注）、`section_index`（同名标题分段，取代语义混乱的 `part`）、`chunk_index`（章内顺序）、`content`、`item_name`（唯一在用的检索过滤）、两个向量。删除 `doc_version`/`content_hash` 即放弃增量重导（重导靠 `chunk_key` 确定性仍不换号，代价是整本重嵌）；删除 `is_active` 即放弃软删除（删文档仍物理删）。顺带修两处：证据扩展排序从 `chunk_id` 数值序改 `chunk_index`（哈希派生号会冒充文档顺序、静默错位）；`MAX_CONTEXT_CHARS_PER_DOC` 1200→2048（893 条里 50 条 1206–1997 字符原被截断，5 条 × 2048 ≈ 7k tokens，不到 qwen-plus 128k 档的 6%） | 2026-11-17 落地；字段类型收紧（`content` VARCHAR(8192)、标题类 VARCHAR(256)）；`_check_chunks_schema` 改为「恰好 12 字段」并拒绝多余列；当天晚些集合名归一为 `kb_chunks`（见 D26） |
| D26 | **切片集合只保留一个名字 `kb_chunks`**，版本号不写进集合名（迁移期的 `kb_chunks_v2` 已并回 `kb_chunks`） | 两个集合并存带来三重成本：配置/代码/文档双写；每处 grep 都要先判断「这个 `kb_chunks` 指新旧哪个」；新旧 schema 字段不同，读代码的人会以为还在跑旧表。schema 精简完成后，回滚窗口的意义已经消失。集合名带版本号还会让人误以为 v1 仍在役，所以宁可做一次「校验 → drop 旧 → 重建同名 → 拷回 → 校验 → drop v2」的搬迁（Milvus 不支持 rename） | 2026-11-17 落地；`scripts/consolidate_chunks_collection.py`（带 `--dry-run`，校验不过就不删 v2）；`milvus_config.chunks_collection` 默认 `kb_chunks`；`scripts/migrate_chunks_v2.py` 已删除 |
| D27 | **检索充分性判定 + 一次换策略补检：能力保留、默认关闭**：每轮都调判定器（不再只在结构红灯时调），不足则**换检索词补检一次**；仍不足时把 `missing` 交给生成侧，要求明说「手册没写这里」 | 复测的答案级幻觉率 0.7213（88/122）主因是「证据像但不覆盖，模型仍把话说满」，而不是召回为零。结构红灯（无证据/纯联网/重排降级/本地<3）抓不到这种情况；拿重排 top1 分数当门槛也不成立——实测与幻觉几乎不相关（`<0.8` 桶 6/7 例有幻觉、claim 率 0.205；`>=0.97` 桶 45/59 例、0.132），按分数设红灯只会白烧调用。所以要**语义判定**，并用三重护栏控制代价：轮次上限 2、剩余预算 <45s 不再补检、耗尽时回落到第一轮证据（防改写把答案带偏）。第三层才是治幻觉的关键：不足要说出来，而不是让模型自由补空。**但当天实测没看到收益，故默认关**：144 例（topk 10 + 2 轮）claim 级 0.1550 / 答案级 0.7857 / pass 0.4127；同批 36 例只改这一个开关时，开 = 0.1639 / 0.8125 / 0.375，关 = 0.1364 / 0.8438 / 0.469，逐例 8 更少 / 9 更多 / 19 持平 | 2026-11-17 落地并当日回退默认；`RETRIEVAL_MAX_ROUNDS=1` + `RETRIEVAL_JUDGE_ALWAYS=false`（打开时设 2 / true）；`node_answer_output` 新增 `_with_insufficiency_note`（判定为 retry 且转入作答时，上下文末尾追加「检索说明」）；回归用例 `_check_gate_routing_and_evidence` / `_check_insufficiency_note`；结论与数字见 HANDOFF §3.102 |
| D28 | **改写层「prompt 约束 + 确定性守卫」双保险**：模型改写后仍要过一遍「历史型号是否丢失」的规则校验，丢了就把历史里最后一次出现的型号原样前置 | 纯粹靠 prompt 在多轮指代上会失败（实测 anaphora 4/4 全拒答，模型把「它」理解成句子里的部件名，改写结果只剩「烫金膜盒如何安装？」）。规则守卫只做一件事：**从历史原文里搬运型号**，不猜产品、不补型号；当前问题自己写了型号时不干预（与 D21「显式型号优先于部件映射」同一取向）。这样「一次糟糕的改写」最多是不生效，不会引入错误产品 | 2026-11-17 落地；`prompts/rewrite_query.prompt` 加反例示例；`node_item_name_confirm._keep_history_subject`；回归用例 `_check_rewrite_keeps_subject`；已知局限：只识别带字母数字的型号，纯中文产品名仍靠 prompt。2026-11-18 扩展：省略主语的续问（「怎么进行烫金」）也走 `_history_subject_hint` 补回任务描述里的历史型号，不再要求出现代词或改写发生变化 |
| D29 | **终稿接地校验 + 事实台账**：主 agent 收集本轮工具/子 agent 返回作为台账；高风险 token 先做确定性核对，必要时再跑一次 flash 校验与一次基于台账的修复 | 用户可见的是主 agent 转述稿，子 agent 即使 grounded，转述仍可能新增细节；离线 judge 无法在运行时拦截。先做确定性价格/订单号/型号核对（零模型调用），只在有实质工具返回时才跑 LLM；修复只能删/降级/基于台账重写，不允许自己补事实；失败 fail-open，避免把能答的题拖成错误 | 2026-11-18 落地；`app/agent/runtime/grounding.py` + `reply_verifier` / `reply_repair` 提示词；`query_runs.grounding` 记录；单轮最多 2 次额外调用，纳入整轮预算 |
| D30 | **幻觉指标拆分为 unsupported / contradicted / core / 严格 any-claim 四项** | 最新 126 条有 claim 的答案平均 11.78 条 claim；claim 级 15.5%，按「任一 claim 无支撑」聚合得到 78.6%，但其中只有 11 条（8.7%）含 contradicted。只报严格答案级会掩盖「多写一个外观细节」与「核心步骤答错」的区别，也会让优化方向偏向缩短答案而不是提高接地性 | 2026-11-18 落地；judge prompt 增加 `core` 标记，`aggregate` 增加 `unsupported_answer_rate` / `contradicted_answer_rate` / `core_hallucination_*` / `claims_per_answer_mean`；旧 JSONL 兼容（无 core 时为 None） |
| D31 | **多轮产品主体改为结构化 `history_product`**：主 agent 从真实会话历史解析最近一个确认产品，通过 ContextVar 传给 product-kb；子图只在当前问题没有锁定产品时使用 | 让子图从自然语言 task 描述里用正则搬型号，能覆盖带字母数字的型号，但覆盖不了纯中文产品名，也容易把主 agent 推断出的产品当成用户说的。结构化字段来自真实历史 + `product_resolver`，可审计；当前问题已确认产品/出现候选时不覆盖，保证用户显式指定优先 | 2026-11-19 落地；`main_agent._find_history_product`、`_HISTORY_PRODUCT`；`QueryGraphState.history_product`；`node_item_name_confirm._with_history_product`；task 描述回填保留为兼容兜底 |
| D32 | **评测 gold 用章节标题重映射，并做同批 3 次重复**：知识库重建后按 `file_title + 章节标题` 把旧 gold 映射到新切片；同一批 20 例跑 3 次，报告均值/方差；不重新导入用户主动删除的文档 | chunk_id 会随重建变化，旧 gold 静默失效会让 Recall 看起来「变差」；单次 judge 受采样噪声影响，不能凭一轮下结论。标题映射保留人工标注的语义，比按位置/顺序猜更可靠；用户已删除的 Legion/GANSS 文档不应为凑样本重新导入 | 2026-11-19 落地；`eval/cases.yaml` HL-3040 gold 重映射；回归样本改用当前库万用表/烫金机；3×20 结果与校准见 HANDOFF §3.106 |
| D33 | **召回图片内联展示先走前端块级渲染，不扩 proto**：`messageBlocks` 把图片作为 block 插到「图 N」引用行后，打字机随文字逐块展示 | 当前前端开启流式时实际忽略 `delta`，等 final 后打字机展示；因此提前发 `images` 事件不会产生可见效果，只会增加双方契约与多端映射成本。final 的 `images` 已按引用顺序重编号，前端足以完成行/段落级内联；真 token 级图文流需要早期事件 + final 重映射，留作后续独立需求 | 2026-11-19 落地；`ItsMyGo/frontend/src/components/chat/messageBlocks.ts` / `AgentChatPanel.vue`；不改 `proto/agent.proto`；已知限制：多派发图片编号、DOM 句内切分未做 |
| D34 | **章节路径在切分层生成 `heading_path`，`parent_title` / `chunk_key` 保持原语义** | 当前 `parent_title` 是章节分组键，被 `(file_title,parent_title)` 回查和 `chunk_key` 依赖；直接改成“父标题”会让章节扩展错组、全部切片换 key、gold 失效。单独的 `heading_path` 只用于 `section_path` 展示，既不改变检索过滤，也不改变身份；缺失时回退旧公式，兼容旧数据 | 2026-11-19 落地；`node_document_split` 标题栈、`chunk_identity.build_chunk_fields` 优先 `heading_path`；现有集合需重新导入或按源 Markdown 回填后才看到新路径；回归 `_check_heading_path` / `_check_chunk_identity` |
| D35 | **生成策略按问题类型路由，core 幻觉作主口径**：确定性规则把问题分为 steps / descriptive / fact / general，`answer_out.prompt` 注入对应约束；judge 报告把 core 放在严格 any-claim 前 | 同一套 prompt 对步骤类（容易补前置条件/排错分支）、事实类（容易套同系列参数）、描述类（容易补外观/场景）无法同时收紧；确定性分类零额外调用、可测试。core claim 更接近“答错关键事实”的用户风险，strict any-claim 会被长答案和外观细节淹没；两者都保留，core 主看、strict 辅助 | 2026-11-21 落地；`node_answer_output.classify_answer_mode` / `ANSWER_MODE_RULES`、`query_graph_default_state.answer_mode`、judge 报告与 CLI；回归 `_check_answer_mode_classification`；效果见 HANDOFF §3.109 |
| D36 | **所有提示词资产统一放根目录 `prompts/*.prompt`**，代码通过 `load_prompt` 读取；subagent 的 description/system prompt、answer_out/rewrite/judge 的 system prompt 全部外置 | 提示词散落在 `main_agent.py` 与节点代码里时，改文案要改代码、review 看不到 diff 的全貌，也无法统一做指纹/注入检查。根目录 `prompts/` 已是既有约定，保持扁平命名即可；工具 docstring 是 LangChain schema 的一部分，仍留在工具旁。迁移只搬文本、不改行为，逐份按去空白字符比对 | 2026-11-22 落地；新增 10 个 `.prompt`；`_check_prompt_assets_externalized`、judge/answer_out/rewrite 的来源断言；requirements 2.34 |
| D37 | **四个 subagent 统一 `agent/api/application/domain/infrastructure` 骨架，配置统一放 `app/conf`**：声明式 agent 用 Service + Protocol 端口 + 构造注入；图型 agent 的图/节点放 application，state 放 domain | `main_agent.py` 同时持有四份配置，共享 `app/agent/tools` 又混入各 agent 的专用工具，导致耦合和职责不清。统一骨架让每个 agent 自包含、工具适配与业务服务分离；可调项集中在 `app/conf`，避免每个 agent 再建一份 `config/` 造成配置分散。但 Python 不搬 Java 的 getter/setter/Factory 样板，图节点仍保留函数形态（LangGraph 直接调用） | 2026-11-22 落地；四份 agent.py + api/tools + application/domain/infrastructure；导购配置收敛到 `app/conf/product_guide_config.py`，agent 内 `config/` 目录已删除；`_rag_task` 因依赖 runtime ContextVar 暂留 `runtime/main_agent.py`；回归全绿 |
| D38 | **认证之后补授权边界：路径只允许安全 basename、HTTP 会话按 `user_id` 归属、管理端写接口要求 admin** | `current_tenant` 原先只认证不授权；上传文件名与 `file_title` 直接拼路径；HTTP 会话统一落 `"local"`。单机演示时被掩盖，一旦有多把密钥或端口暴露，就是任意文件写/删与跨用户读。做法是收边界而不改业务：路径统一 `resolve()` + `is_relative_to(OUTPUT_DIR)`；`verify_key` 返回 Mongo `_id` 作为 HTTP `user_id`；admin 跨用户、viewer 只看自己；MinIO 端口默认回环、默认口令告警；Dockerfile 补 `scripts/` | 2026-11-22 落地；`safe_filename` / `validate_file_title` / `require_admin` / 会话 user_id 选择；回归新增 7 条；MinIO 匿名只读与 gRPC 身份绑定留后续跨仓改造 |

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
