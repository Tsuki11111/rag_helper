# 掌柜智库 · 产品文档知识库 RAG

基于 **LangGraph** 的多路检索 RAG 系统，面向**产品使用文档**（说明书、用户手册）的导入与问答。

两条独立的图：
- **导入图**：PDF/MD → 解析 → 读图 → 切分 → 产品主体识别 → 向量化 → 入 Milvus
- **检索图**：产品确认 → 四路并行检索 → RRF 融合 → 重排 → 生成答案

配套两个 FastAPI 服务和两个前端页面，问答侧用 SSE 实时推送检索过程与流式答案。

---

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph |
| 大模型 | 通义千问（OpenAI 兼容接口，`langchain-openai`） |
| 嵌入 | DashScope `text-embedding-v2`（1536 维，**仅稠密向量**） |
| 重排 | DashScope `gte-rerank-v2`（**原生端点，非 OpenAI 兼容**） |
| 向量库 | Milvus 2.5（standalone） |
| 对象存储 | MinIO |
| 会话历史 / 去重记录 | MongoDB |
| 知识图谱 | Neo4j 5.26（Community，本地 Docker） |
| 图谱可视化 | ECharts 5.4.3（**CDN 引入**，三源回退 + 文本降级） |
| PDF 解析 | MinerU（云端 API） |
| 联网搜索 | 百炼 MCP `EnhancedSearch`（openai-agents 客户端，**`mcp<2`**） |
| Web | FastAPI + 原生 HTML/JS（无框架） |
| 包管理 | uv（Python ≥ 3.12） |

---

## 目录结构

```
app/
├── clients/                 # 外部服务客户端
│   ├── milvus_utils.py      # Milvus 单例、稠密检索、产品名写入
│   ├── minio_utils.py       # MinIO 客户端
│   ├── mongo_history_utils.py   # 会话历史读写
│   ├── mongo_dedup_utils.py     # 上传去重指纹
│   ├── mongo_usage_utils.py     # 调用账本（每次模型调用的 tokens/延迟/成本）+ 报表
│   ├── mcp_search_utils.py  # 百炼 MCP 联网搜索（Streamable HTTP）
│   └── neo4j_utils.py       # Neo4j 知识图谱读写、幂等清理、图谱统计
├── conf/                    # 各服务的配置类（读 .env），含 pricing_config.py（模型计价表）
├── core/                    # 日志（人读文本 + 机器读 JSONL + log_query 查询入口）、提示词加载、
│                            #   request_context（请求归因）、usage_tracker（记账）、error_policy（异常分级）
├── lm/                      # LLM 客户端、嵌入、重排
├── import_process/          # ── 导入链路 ──
│   ├── agent/main_graph.py      # 导入图编排 + 端到端测试
│   ├── agent/nodes/             # 8 个节点
│   ├── agent/create_collections.py  # Milvus 建表
│   ├── api/file_import_service.py   # 上传服务（含去重、撤回）
│   └── page/import.html         # 上传页面
├── query_process/           # ── 检索链路 ──
│   ├── agent/main_graph.py      # 检索图编排 + 流程测试
│   ├── agent/nodes/             # 8 个节点
│   ├── api/query_service.py     # 查询服务（SSE + 历史接口）
│   └── page/chat.html           # 问答页面
└── utils/                   # 任务追踪、SSE、哈希、文档管理等
prompts/                     # 提示词模板（.prompt）
doc/                         # 待导入的原始 PDF
output/                      # 导入过程的中间产物（按文档隔离）
docker/milvus-compose.yml    # Milvus standalone 编排
```

---

## 进度总览

### 导入链路 —— 已完成 ✅

8 个节点全部实现并端到端验证通过（实测导入 371 切片的 PDF 耗时约 350 秒）。

| 节点 | 状态 | 说明 |
|---|---|---|
| `node_entry` | ✅ | 按后缀路由 PDF/MD，提取 file_title |
| `node_pdf_to_md` | ✅ | MinerU 云端解析，下载解压 md |
| `node_md_img` | ✅ | 图片上传 MinIO + 视觉模型生成描述，替换 md 中的图片链接 |
| `node_document_split` | ✅ | 按 Markdown 标题层级切分，长切片二次切分、短切片合并 |
| `node_item_name_recognition` | ✅ | 大模型识别产品主体 → 写 `kb_item_names` + 生成向量 |
| `node_dashscope_embedding` | ✅ | 批量生成切片稠密向量 |
| `node_import_milvus` | ✅ | 校验 → 幂等清理 → 批量插入 `kb_chunks` → 回填 chunk_id |
| `node_import_kg` | ✅ | 切片分批交 LLM 抽实体+关系 → 写入 Neo4j（含切片挂载）；Neo4j 不可用时跳过不中断导入 |

**导入服务**（`file_import_service.py`，端口 **8001**）已完成：

- `POST /login` / `POST /logout` —— 用访问密钥换 / 清 HttpOnly 会话 Cookie
- `POST /upload` —— 上传 + **SHA-256 内容去重**（改名仍能识别）+ `force=true` 强制重导
- `GET /documents` —— 已导入文档列表（以 Milvus 为主聚合，附图谱实体数）
- `GET /documents/graph?file_title=` —— 单文档知识图谱（实体 + 关系，供前端可视化）
- `DELETE /documents/{file_title}` —— **撤回**（清 Milvus 切片与产品名、Neo4j 图谱、去重记录、本地产物、MinIO 对象），带二次确认
- 前端 [import.html](app/import_process/page/import.html)：拖拽上传、重复提示、撤回交互、昼夜模式、**知识图谱可视化**（点文档行的「图谱」按钮，力导向图展示实体关系，支持按类型筛选与搜索）

### 检索链路 —— 部分完成 🚧

8 个节点全部完成 ✅

| 节点 | 状态 | 说明 |
|---|---|---|
| `node_item_name_confirm` | ✅ | 7 步完整：LLM 提取产品名+改写问题 → 向量对齐 → 三分支（确认/反问/拒识） → 写历史 |
| `node_search_embedding` | ✅ | 改写问题 → 向量化 → `dense_search`（带 `item_name` 过滤）→ `embedding_chunks`；单节点实测 Top1 0.64 |
| `node_search_embedding_hyde` | ✅ | LLM 生成假设文档 → 「问题+假设文档」向量化 → 检索 → `hyde_embedding_chunks` + `hyde_doc`；单节点实测 Top1 0.71 |
| `node_web_search_mcp` | ✅ | 异步调百炼 MCP 增强搜索（工具 `search_pro`）→ `web_search_docs`；图内实测返回 5 条。**可被前端的「联网」开关关掉**（`enable_web_search=false` 时上来就返回空，但节点照走，四路 fan-in 不能少一条） |
| `node_query_kg` | ✅ | 图谱检索：问题里的实体 → 种子 + 一跳邻居 → 取回切片（正文回 Milvus 取）；图内实测召回 5 条，4 条进 RRF |
| `node_rrf` | ✅ | 加权 RRF 融合切片类召回（基线 / HyDE / 图谱，k=60）→ `rrf_chunks`；图内实测 5+5 输入去重融合为 6 条 |
| `node_rerank` | ✅ | 合并本地切片 + 联网结果为统一格式 → DashScope 重排打分 → 动态 Top-K（断崖截断）→ **给本地切片留配额** → `reranked_docs`；图内实测 6+5 输入输出 8 条 |
| `node_answer_output` | ✅ | 用 `reranked_docs` 组装上下文调 LLM 生成答案；流式逐块推送，解析【图片】区块作为配图；答案存档 |

**查询服务**（`query_service.py`，端口 **8002**）已完成：

- `POST /login` / `POST /logout` —— 用访问密钥换 / 清 HttpOnly 会话 Cookie
- `POST /query` —— 提交问题（流式返回 session_id / 非流式直接返回 `answer` + `images` + `usage`）。
  可用 `enable_web_search: false` 关掉联网那一路（只从知识库与图谱取参考内容）；默认 `true`
- `GET /stream/{session_id}` —— **SSE** 推送 `ready` / `progress` / `delta` / **`usage`** / `final` / `error`
  （`final` 带 `answer` 与 `images`；`usage` 每记完一笔就推一次累计用量，见[「调用记账」](#调用记账)）
- `GET /sessions` —— **会话列表**（按最近活跃倒序，给前端左侧会话栏用）；
  这是唯一一个「跨会话」的读取接口，其余都要先知道 `session_id`
- `GET /history/{session_id}`、`DELETE /history/{session_id}` —— 会话历史查询与清空
  （返回的消息带 `images`：`[{"url", "caption"}]`，图注一并存下来，重新打开时图卡不会退化成「未标注来源」）
- 前端 [chat.html](app/query_process/page/chat.html)：**左侧会话栏**（切换 / 新建 / 删除）、
  检索管线可视化、流式答案（**Markdown 渲染**）、**答案配图**、昼夜模式；
  **刷新后接着聊** —— `sessionId` 存 localStorage，启动时拉 `/history` 把历史连图一起还原

### 未开始 ⬜

功能节点暂无待办——导入链路与检索链路共 16 个节点均已实现并验证。

**企业化改造**（可观测性、Durable Execution、多租户、治理）的路线与逐项进度见下面
「企业化改造路线」一节。

零散的后续方向：给图谱侧排序做更可靠的权重、把其余 4 份文档重新导入以生成图谱、答案配图做去重与缩略图。

---

## 企业化改造路线（待办与进度）

参考 [《Zero2Agent · Agent Infra》](https://onefly.top/zero2Agent/learn-agent-basic/09-agent-infra/index.html)
的六层标准。**现状**：Harness 层已相当完整（16 个节点、四路召回、图谱、MCP），
但 Infra 层几乎是空的——对照文中递进（Demo → 内部工具 → **面向用户产品** → 企业平台 → 强合规），
目前处在中间偏下。

> **本节是路线的唯一权威**，HANDOFF 只留指针，避免两份文档各自演化。
> 当前进度：**16 项完成 6 项**（Phase 1 四项 + Phase 2 的 checkpointer、单节点超时与预算）。

文中两个判断值得记住：
「**Checkpoint + Durable Execution 是从 Demo 到生产最关键一步**」，
以及「多租户、成本归因、合规审计、Guardrails、灰度占据企业 Infra 70% 以上工作量」。

### 现在就存在的三个问题

这三条是后面所有工作的动机，也是判断优先级时的依据。

| 问题 | 现状 |
|---|---|
| ~~**异常被 `except` 吞掉**~~ | **✅ 已解决**，见[「异常分级处置」](#异常分级处置)：实现 `node_query_kg` 时一个 `NameError` 被「失败不中断链路」的兜底 `except` 降级成 warning，图谱那一路静默返回空、功能等于废了，只有测试断言才发现。现在编程错误会**上抛**，外部故障降级但带 `degraded` 标记、可用 `log_query --degraded` 查出来 |
| **任务状态在内存里** | `task_utils` 用普通 dict 存运行/完成列表，**服务一重启，进行中的导入就凭空消失** |
| ~~**成本完全不可见**~~ | **✅ 已解决**，见[「调用记账」](#调用记账)：一次问答要调 LLM + 四路召回 + 重排 + 图谱 + MCP，此前花了多少、谁花的账上一片空白；现在每次调用逐笔入账，可按类型 / 模型 / 租户 / 单次请求归集 |

### Phase 1 · 地基（最该先做）

- [x] **访问鉴权与租户标识** —— ✅ 已完成，见[「访问鉴权」](#访问鉴权)一节：
      API Key + HttpOnly Cookie，9 个数据接口全部受保护。
      **但只做认证、不做数据隔离**：密钥带 `tenant_id`，数据尚未按它过滤
- [x] **单次调用记账** —— ✅ 已完成，见[「调用记账」](#调用记账)一节：
      LangChain callback 采集 LLM 用量 + 三处手工埋点（嵌入 / 重排 / 联网搜索），
      逐笔写 MongoDB `llm_usage`，可按类型 / 模型 / 租户 / 单次请求出报表。
      `tenant_id` 已经进账本，为 Phase 3 的成本归因留好了接口
- [x] **结构化日志** —— ✅ 已完成，见[「结构化日志」](#结构化日志)一节：
      控制台保留人读格式并挂上 `[trace·node]` 标签，另存一份 JSONL 给机器查；
      归因字段复用记账那套 `request_context`，业务代码一行没改
- [x] **异常分级处置** —— ✅ 已完成，见[「异常分级处置」](#异常分级处置)一节：
      编程错误上抛、外部故障按可重试/需人工分类降级，所有降级带 `degraded` 标记可统计。
      重试策略按路线留在 Phase 2

### Phase 2 · 可靠性

- [x] **LangGraph checkpointer** —— **两张图都接了**（MongoDB），见[「图检查点」](#图检查点checkpointer)一节：
      `MongoDBSaver` + 7 天 TTL + 内存降级；`thread_id` 一轮一个（查询用 run_id、导入用 `import_<task_id>`）。
      **导入侧还带「重启自动续跑」**：服务启动时扫描没跑完的导入线程接着跑完，MinerU 那几分钟不白费。
      在此之上落了[「产品确认中断」](#产品确认中断图主动中断)：图认不出产品时主动中断、前端弹卡片、
      选完 resume 接着跑 —— 这是 checkpointer 的第一个真实用途
- [ ] 故障分类重试：timeout 有限重试、429 退避、refusal 不盲重试、invalid tool args 绝不执行
      —— 分类已就绪（见[「异常分级处置」](#异常分级处置)，`retryable` 已标好），只差重试策略。
      **动手前先用 `log_query --degraded` 统计几天的错误分布**，别拍脑袋定次数与退避
- [x] 单节点超时 + 整个查询的 wall-clock / token 预算 —— 见[「超时与预算」](#超时与预算)一节：
      **超时落在各客户端**（框架的节点级超时对同步图不可用，实测过），
      **预算落在 `tracked_node` 包装层**（两张图所有节点必经，一处生效），只作用于查询图
- [ ] `task_utils` 从内存搬到 Redis / Postgres

### Phase 3 · 多租户与观测

- [ ] 四个存储加 `tenant_id`：Milvus（**要重建集合**）、Neo4j、MongoDB、MinIO 路径前缀
- [ ] per-tenant 并发槽位、token 预算、工具频率限制
- [ ] 分布式 trace（OpenTelemetry / Langfuse）
- [ ] 成本归因与分摊

### Phase 4 · 治理

- [ ] Guardrails：输入护栏、输出护栏、工具护栏
- [ ] append-only 审计日志（`who` / `what` / `risk_score` / `approver`）
- [ ] 高危操作人工审批 —— 「撤回文档」就是典型场景
- [ ] Agent 版本管理（prompt hash + tool set hash + model version）与灰度发布

### 优先级建议

**别按顺序全做。** 文中那张递进表本质是在说：做到「面向用户产品」那一档就已经拿到 80% 的收益，
多租户 + chargeback + 灰度那一档是给多团队平台准备的，单人 / 小团队做进去性价比很低。

**Phase 1 的前两项无论如何先做**——它们不依赖任何架构决策，而且做完之后才有数据判断后续该往哪投。

---

## 评估路线（待办与进度）

参考 [《Zero2Agent · Agent 评估》](https://onefly.top/zero2Agent/learn-agent-basic/11-agent-evaluation/index.html)
的四维度（任务完成率 / 效率 / 安全合规 / 鲁棒性）与四方法（固定测试集 / LLM-as-Judge / A/B / 对抗测试）。
**现状**：结构化日志、调用账本、`log_query` 已经有了**看得见**的三层数据，
但**判断**的能力还没建起来——没有固定用例、没有聚合与趋势、也没有答案质量的采集。

> **本节是路线的唯一权威**，HANDOFF 只留指针，避免两份文档各自演化。
> 当前进度：**6 个阶段完成 0 个**。指标定义、测试集设计、方法矩阵、门禁与局限等**执行细节**
> 见 [evaluation-plan.md](evaluation-plan.md)。

文中几个判断值得记住：
「**持续监控比一次性上线前评测更重要**」、
「**至少 30% 的用例该覆盖异常与边界**」，
以及「LLM-as-Judge **别用同一个模型既当运动员又当裁判**」。

### 它做不到什么

这一条必须先说清，免得对它期待错。

**它发现不了新问题。** 2026-10-06 一天修掉 8 个 bug，**一个都不是测试发现的**——
全是真实提问 + 人肉读日志挖出来的。所以评估的作用是**把已知问题锁住、让改动可比较**，
不是「测出未知的坏」。**这也决定了种子用例集从哪来**：就是自己系统曾经坏过的那些，
不是公开 benchmark（教程把「迷信公开 benchmark」列进了四个坑）。

**改造让系统更可靠，评估让系统可判断。** 前者回答「它坏了吗」，后者回答
「**它比昨天好了吗**」——后者是唯一能回答「这次改动到底值不值」的那个。

### 拆成 6 个环节，而不是一个端到端指标

「答案不对」既可能是产品名没认出来、也可能是召回没捞到、也可能是重排排错——不拆就没法定位。
2026-10-06 那三个检索 bug（本地归零 / 被联网挤掉 / 切片重复）**全都只有拆开才看得见**。

| 环节 | 关键指标 | 靠什么断言 |
|---|---|---|
| E1 意图与产品名 | 对齐准确率 | 纯代码 |
| E2 召回 | Gold Recall@K / 本地命中率 / 重复率 | 纯代码（需 gold 标注） |
| E3 排序 | MRR / 来源构成 | 纯代码 |
| E4 生成 | 接地性 / 拒答正确率 / 配图准确率 | LLM-as-Judge + 纯代码 |
| E5 流程与韧性 | 降级路数 / 中断恢复 | 纯代码 |
| E6 系统 | 成本 / P50、P95 / 成功率 | 纯代码 |

端到端指标单独保留一条——「各环节都对、合起来不对」是真实存在的。

### P0 · 运行记录落库（评测的地基）

- [ ] **一轮问答的「运行记录」落库** —— 评测的**输入**：
      `done_list` / `degraded_list` / 本轮 `usage` 现在只推给前端就丢了，
      没有它，下面所有聚合都只能去解析日志文本

### P1 · 把已知问题固化成离线用例（最该先做）

- [ ] **把那批静默 bug 写成离线纯断言用例** —— **当天就能验证一个假设**：
      纯代码断言到底能不能挡住它们？能挡就说明 P2 / P3 可以慢慢来
- 第 1 批**已经落地**：见[「回归用例」](#回归用例)（11 条，含 mutation 验证）

### P2 · gold 标注

- [ ] **人工标注 `gold_chunks`** —— 这是全流程**唯一无法自动化**的一步，也最花时间；
      没有它，`Gold Recall@K` 与 `MRR` 都是假的，只能靠 judge 猜

### P3 · LLM-as-Judge

- [ ] **接地性与答案相关性的自动打分** —— 必须**换一个模型**当裁判，且裁判本身要人工抽样校准
- 放在 P2 之后：现在**没有一个「答案质量」的决策要做**，等真要做「换个提示词到底有没有变好」时再上

### P4 · 对比报告与门禁

- [ ] **base→head 的差值表**：通过率 / Recall / 本地命中率 / 成本 / P95
- [ ] 接进日常：改完跑 smoke，定期跑 full

### P5 · 对抗与防过拟合

- [ ] **对抗用例**（注入、越权、超长上下文）—— 版本发布前跑
- [ ] **shadow 模式**（真实流量旁路）—— 防测试集过拟合

### 明确不做的三条

- **把评估并进「企业化改造」的 Phase** —— 它是独立轨道，混进去两边都做不好。
  两条线并行不冲突：改造不依赖评估，而评估的大部分用例**离线就能跑**，不烧模型配额
- **一上来就建平台** —— 教程原话：先小测试集 + 自动化，而不是一步到位上平台
- **现在就上 P2 / P3** —— 见上面两节的理由；P1 跑通之前，那些都是在回答还没有的问题

---

## 快速开始

### 1. 安装依赖（uv）

依赖由 **uv** 管理（`pyproject.toml` + `uv.lock`）。`uv sync` 按 lock 文件把 `.venv/` 补齐：

```bash
uv sync
```

注意它是**精确同步** —— 除了装上缺的包，还会**卸载 lock 里没有的包**（uv 的默认行为，
保证环境与 lock 严格一致）。所以别手改 `.venv/` 里的内容，改了下次 `uv sync` 就被抹掉。

- **不要用 pip 往 `.venv/` 里装包**。这个 venv 是 uv 建的，**里面没有 pip**（`python -m pip` 会报
  `No module named pip`）—— 这是 uv 的默认行为，不是环境坏了
- 新增依赖一律 `uv add <包名>`，它会同时更新 `pyproject.toml` 与 `uv.lock`；**不要手改 lock 文件**
- 下文命令统一写 `.venv/Scripts/python.exe -m ...` 显式调解释器；等价的 `uv run python -m ...` 也可以

### 2. 配置 `.env`

```ini
# ── 大模型（通义千问，OpenAI 兼容）──
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_DEFAULT_MODEL=qwen-plus
LLM_DEFAULT_TEMPERATURE=0.1
VL_MODEL=qwen3-vl-flash

# ── 嵌入模型（DashScope）──
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v2
EMBEDDING_DIM=1536
EMBEDDING_BATCH_SIZE=16

# ── 重排模型（DashScope，注意不是 OpenAI 兼容端点）──
# 不设置时复用 OPENAI_API_KEY
RERANK_MODEL=gte-rerank-v2

# ── Milvus ──
MILVUS_URL=http://127.0.0.1:19530
CHUNKS_COLLECTION=kb_chunks
ITEM_NAME_COLLECTION=kb_item_names
MILVUS_METRIC_TYPE=COSINE

# ── MongoDB ──
MONGO_URL=mongodb://127.0.0.1:27017
MONGO_DB_NAME=kb002

# ── Neo4j 知识图谱 ──
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_DATABASE=neo4j
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=123123123

# ── MinIO ──
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET_NAME=knowledge-base-files
MINIO_IMG_DIR=/upload-images

# ── MinerU（PDF 解析）──
MINERU_API_TOKEN=sk-xxx
MINERU_BASE_URL=https://mineru.net/api/v4

# ── 百炼 MCP（联网搜索，供 node_web_search_mcp 使用）──
# 鉴权复用 OPENAI_API_KEY；Streamable HTTP 协议，服务端无状态（响应不带 session-id）
# 目前仅一个工具 search_pro，参数 query
MCP_DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1/mcps/EnhancedSearch/mcp

# ── 超时与预算（都可选，不配就用代码里的默认值，见 app/conf/budget_config.py）──
# 各客户端单次调用超时（秒）。不设的话多数走 SDK 的隐式 600 秒，MinerU 的 requests 更是无限等待
LLM_TIMEOUT_SEC=120
EMBEDDING_TIMEOUT_SEC=60
MILVUS_TIMEOUT_SEC=10
NEO4J_CONNECT_TIMEOUT_SEC=5
NEO4J_TX_RETRY_TIMEOUT_SEC=10
MINIO_CONNECT_TIMEOUT_SEC=5
MINIO_READ_TIMEOUT_SEC=60
MINERU_CONNECT_TIMEOUT_SEC=30
MINERU_READ_TIMEOUT_SEC=60
MINERU_TRANSFER_TIMEOUT_SEC=300
# 整轮预算：只作用于查询图，且**达到即止**（想放宽就调大，别设 0 —— 0 会在第一个节点前中止）
QUERY_WALL_CLOCK_BUDGET_SEC=180
QUERY_TOKEN_BUDGET=80000
```

> ⚠️ **导入文档时要能访问 MinerU 的 CDN，开着代理 / VPN 常常会失败。**
> MinerU 解析完的结果包放在 `cdn-mineru.openxlab.org.cn`，**走代理时到这个域名的 TLS 握手会被掐断**，
> 报的是 `SSLEOFError: UNEXPECTED_EOF_WHILE_READING`（0.4 秒内就失败，**不是超时**）。
> 表现：导入任务跑到 `node_pdf_to_md` 就 `failed`，日志里是「使用MinerU进行PDF的解析时发生错误」。
> **关掉 VPN 再导即可**。
>
> **问答不受影响**：它走 DashScope 的国内接口，实测在 CDN 不可用期间照常问答。

### 3. 启动依赖服务（Docker）

```bash
# Milvus standalone（etcd + milvus，复用已有的 minio）
docker compose -f docker/milvus-compose.yml up -d

# MongoDB
docker run -d --name mongo -p 27017:27017 -v mongo-data:/data/db mongo:8

# Neo4j 知识图谱（独立 compose，自带 project 名，不会与 Milvus 的容器互相干扰）
docker compose -f docker/neo4j-compose.yml up -d
```

> 容器均未设 restart policy，Docker Desktop 重启后需手动拉起：
> `docker start minio milvus-etcd milvus-standalone attu mongo mongo-express neo4j`

### 4. 建 Milvus 集合（首次）

```bash
.venv/Scripts/python.exe -m app.import_process.agent.create_collections
```

### 5. 启动服务

```bash
# 导入服务 → http://127.0.0.1:8001/import.html
.venv/Scripts/python.exe -m app.import_process.api.file_import_service

# 查询服务 → http://127.0.0.1:8002/chat.html
.venv/Scripts/python.exe -m app.query_process.api.query_service
```

### 6. 命令行跑图（调试用）

```bash
# 导入图端到端测试（改 main_graph.py 里的 TEST_PDF_NAME）
.venv/Scripts/python.exe -m app.import_process.agent.main_graph

# 检索图结构测试（验证分叉/合并/条件路由）
.venv/Scripts/python.exe -m app.query_process.agent.main_graph

# 单节点测试
.venv/Scripts/python.exe -m app.query_process.agent.nodes.node_item_name_confirm
```

---

## 访问鉴权

数据接口需要**访问密钥**；页面本身与 `/health` 保持公开。

```bash
# 建第一个用户（密钥只在这一刻打印一次，丢了只能重建）
.venv/Scripts/python.exe -m app.clients.mongo_user_utils add 张三 admin
.venv/Scripts/python.exe -m app.clients.mongo_user_utils list      # 查看
.venv/Scripts/python.exe -m app.clients.mongo_user_utils revoke 张三  # 撤销
```

两种提交方式，任选其一：

- `Authorization: Bearer <key>` —— 给脚本 / 程序化调用
- 浏览器访问任意页面 → 接口返回 401 时自动弹出密钥输入框 → 登录成功后写入 HttpOnly Cookie

**为什么浏览器侧走 Cookie 而不是让前端存密钥发请求头**：查询服务的流式接口用的是 `EventSource`，
而它**无法自定义请求头**——Bearer 头在那条路上根本发不出去。Cookie 由浏览器自动携带，
页面里十几处 `fetch` 与那个 `EventSource` 一行都不用改。

**当前只做「认证」、不做「数据隔离」**：密钥对应一个 `tenant_id`，但数据还没按它隔离
（给 Milvus / Neo4j / MongoDB / MinIO 四个存储加租户维度是停机迁移级别的改动）。
所以**现阶段任何有效密钥都能看到全部数据**。这一层的价值是把身份打通、为后续隔离留好接口。

---

## 调用记账

每一次外部模型调用（LLM 生成 / 嵌入 / 重排 / 联网搜索）都记一笔账，回答「钱花在哪」。

```bash
# 看最近 1 天 / 7 天的账（按类型、模型、租户、最贵的几次请求）
.venv/Scripts/python.exe -m app.clients.mongo_usage_utils
.venv/Scripts/python.exe -m app.clients.mongo_usage_utils 7
```

```python
# 某一次问答的明细（排查「这次怎么这么贵」）
from app.clients.mongo_usage_utils import summarize_trace
summarize_trace("<trace_id>")
```

**页面上也看得到**：每张答案卡片底部有一行「本次消耗」——`↑9.1k ↓1.03k · ¥0.0093 · 8 次调用 · 20.4s`。
流式回答时它**随每次模型调用结束实时跳动**（正在跑时圆点是活的，跑完停跳并补上总耗时），
点「明细」展开还能看到按节点拆分的调用次数 / tokens / 成本——
`rerank` 这类不显眼的环节花掉多少，一眼就能看见。

实现上记账层不碰 SSE：`usage_context(on_usage=...)` 收一个进度回调，
查询服务把「推 `usage` 事件给前端」这件事作为回调传进去，依赖方向保持单向。
非流式路径则直接用 `/query` 响应里的 `usage` 字段。

**首次实测**（一次完整问答，「Brother HAK 180 烫金机怎么安装烫金膜盒？」，
8 次调用、约 1 万 tokens、估算 **0.0095 元**，其中）：

| 环节 | 成本占比 | 说明 |
|---|---|---|
| 生成答案 | 47% | 上下文最大（7 条切片 ≈ 4.7k 字符），且是流式长输出 |
| 重排 | 43% | 15 条候选一起打分，输入 tokens 达 5k |
| HyDE（LLM 生成假设文档 + 嵌入） | 7% | |
| 产品名确认 / 向量检索 | 1.4% | |
| 联网搜索 | 0% | 按次计费、单价未公开，只记次数不计成本 |

单次样本不代表长期分布，但已经能回答一个具体问题：**这条链路里最贵的不是 LLM 生成，
而是「把 15 条候选全喂给重排」**——想降本应从这里下手（比如先粗筛再精排），
而不是去换更便宜的生成模型。

**记账的边界**：MinerU 解析按页数配额计费、与 token 无关，不在账本内。

### 三个设计取舍

**LangChain callback 挂在 LLM 客户端上，而不是各调用点传 handler**
16 个节点里有 8 处模型调用点，逐个传 handler 既侵入又必漏——**漏了还不报错**，
只是账本上少一笔。挂在 `get_llm_client()` 返回的客户端上（它是全局缓存的单例），
一次配置全项目生效，新增节点也自动被覆盖。

**归因走 ContextVar，而不是函数参数**
账本要回答「谁花的」，就得知道这次调用属于哪个租户、哪次会话、哪个节点。这些信息
在服务入口和节点里天然存在，但传到底层 `generate_embeddings()` 要多穿 5 层。
改用 ContextVar 存「当前请求」，入口设一次即可——**关键在于 LangGraph 的并发执行器
提交节点任务前会 `copy_context()`**，所以四路并发检索（各自跑在不同线程）都能读到
同一份归因，实测 8 次调用全部正确落到 `node_rerank` / `node_answer_output` 等节点名下。

节点头上的归因不是每个节点自己写的，而是 `main_graph` 注册时用 `add_tracked_node`
包一层——一张图只有一处需要维护，节点内部一行都不用改。

**账本只把 tokens 当事实，成本是算出来的**
单价会变（qwen-plus 就调过价）。记录里带当时的估算值，但**报表一律按当前计价表重算**，
这样单价更新后历史账目跟着走，而不是冻结在当初的估算上。
计价表在 `app/conf/pricing_config.py`，来源与查询日期逐条写在注释里。

三处刻意的简化都在明面上：qwen-plus 的阶梯计价只取最低档（本项目上下文远小于 128k）；
联网搜索按次计费、单价未公开，只记次数并把成本记为「未计价」而不是 0；
**失败的调用也入账**（`ok=false` + 错误摘要），因为「哪条路经常挂」只有失败记录能回答。

### 两个实现上的坑

**流式必须显式开 `stream_usage`**
`answer_output` 用 `llm.stream()` 生成答案，这是单次最贵的调用。而 langchain-openai
只在 base_url 是 OpenAI 官方地址时才默认开启 `stream_usage`——本项目指向 DashScope，
不显式传 `stream_usage=True` 就拿不到用量，**最贵的那笔账会静默漏记**。
已在 `lm_utils.get_llm_client()` 里显式打开，并实测 DashScope 兼容端点会在最后一个
chunk 返回 usage。

**记账绝不能反过来弄坏主流程**
所有落库与用量解析都吞异常并降级为 warning：一次账单写不进去，不该让用户拿不到答案。
回调里抛异常还会污染 LangChain 的调用链，`TokenUsageCallback` 的两个回调体都整体兜了。

**数据库挂了也不能拖慢业务**：Mongo 连接超时收到 2 秒（pymongo 默认 30 秒），
且失败后熔断 60 秒——否则每次模型调用都要重撞一遍连接超时。
实测库不可用时首笔 2.8s、后续每笔 0.004s（改前每笔 30s）。
熔断期结束会自动重试，不会永久放弃记账。

---

## 结构化日志

**同一条日志记录，三种渲染，各给各的读者**——不会各记各的，因为没有第二份数据源：

| 输出 | 格式 | 给谁 |
|---|---|---|
| 控制台 | 彩色人读，行首挂 `[9b2063cb·node_rerank]` 标签 | 开发时盯着看 |
| `logs/app_年月日.log` | 同上但不带颜色 | `grep` / 翻历史 |
| `logs/app_年月日.jsonl` | 每行一个 JSON 对象 | 程序、`jq`、将来接采集 |

```bash
# 看某一次请求的全部日志（一次问答 = 一个 trace，与账本里的 trace_id 相同）
.venv/Scripts/python.exe -m app.core.log_query --trace 3dd63c53004e43c2

# 只看图检索节点出的问题，回看最近 3 天
.venv/Scripts/python.exe -m app.core.log_query --node node_query_kg --level ERROR --days 3

# 按关键词搜消息
.venv/Scripts/python.exe -m app.core.log_query --grep 触发断崖 --days 7 --limit 50

# 原样输出 JSON，喂给别的工具
.venv/Scripts/python.exe -m app.core.log_query --trace 3dd63c53004e43c2 --json
```

查询入口刻意做成了**命令行模块而不是只写 jq 用法**：本机没装 jq，只在文档里贴 jq 命令
等于给了一个跑不通的示例。装过 jq 的话，等价的写法是
`jq -c 'select(.trace_id=="...")' logs/*.jsonl`。

**字段固定**：`ts` / `level` / `module` / `function` / `line` / `message`
\+ 归因四件套 `trace_id` / `tenant_id` / `session_id` / `node`
\+ 业务自己 bind 的 `extra` ＋（有异常时）`exception{type,message,traceback}`。
归因字段**一定存在**，不在请求里时是空串而不是 `null`——这样 `select(.trace_id != "")` 这类查询不用额外判空。

**消息开头重复的 `[节点名]` / `[函数名]` 会被自动去掉**
节点代码沿用了 `logger.info(f"[{NODE_NAME}] [{function_name}] ...")` 的写法，而在**入口函数**里
这两个名字恰好相同——消息自己就重复了一遍，再加上行首标签与 `module:function`，
一整行里节点名能出现四次。实测**图内 41% 的日志如此**。现在补丁会剥掉开头连续的
`[节点名]` / `[函数名]`：

```
改前：INFO | [59a0349a·node_item_name_confirm] node_item_name_confirm.py:node_item_name_confirm:382 - [node_item_name_confirm] [node_item_name_confirm] 开始处理
改后：INFO | [59a0349a·node_item_name_confirm] node_item_name_confirm.py:node_item_name_confirm:382 - 开始处理
```

**为什么在补丁里收口、而不是去改 230 处 f-string**：一处生效、新节点自动受益，
节点代码也不必为了日志好看而扭曲写法。只剥**开头连续**的、且内容确实等于当前节点名或
函数名的方括号——正文中间的 `[重要]`、`[图片]` 一律不动。实测改前 41% → 改后 0%。

**两种归因来自两个地方**，缺一个就只剩半个标签：`node` 由 `add_tracked_node` 在注册节点时包上，
**只要走图就有**；`trace_id` / `tenant_id` / `session_id` 由 `usage_context` 在**入口**设置。
所以五个入口（2 个服务 + 3 个命令行跑图的地方）都包了它——
命令行入口漏包过一次，症状是日志里 `[node_rerank]` 有节点名却没 trace，账本里那几笔也归不到哪一次运行。
**新增入口时照做。**

### 三个设计取舍

**归因字段复用记账那套 `request_context`，不另造 ID**
`trace_id` 在[「调用记账」](#调用记账)里已经生成，日志直接读同一个 ContextVar。
好处是**日志与账目天然对得上**：`jq` 出一个 trace 的日志、`summarize_trace()` 出同一条 trace 的账，
两边拼起来就是「这次问答每一步说了什么、花了多少」。

**注入靠补丁，不靠调用点自觉**
`logger.py` 里一个 `enrich_record` 补丁统一附着字段，**16 个节点、几十处 `logger.info` 一行都没改**。
让调用方自己带上下文是行不通的——漏了不报错，只是那几行日志悄悄少了两列。
补丁顺带把「定位真实调用位置」的栈遍历合并进来，两个效果只走一次栈。

**没有把文本日志换成 JSONL，而是并存**
出问题时人肉读 JSON 很难受，机器读彩色文本同样难受。代价是多写一个文件——
本项目日志量很小（一次问答百来行），这点 I/O 换两种读者都舒服，值得。
`LOG_JSON_ENABLE=False` 可单独关掉。

### 三个 loguru 的坑（改 `logger.py` 前必读）

JSONL 那一份**不是**在 `format` 里拼出来的，而是在补丁里预渲染好、`format` 只写一个 `{jsonl}` 占位符。
这不是绕远路，是因为 loguru 会把**可调用 format 的返回值再当模板解析一遍**，内容一旦进了模板就连踩两坑：

1. JSON 的花括号被当字段名 → `KeyError: '"ts"'`
2. 正文里的 `<frozen runpy>`、`<class 'ValueError'>` 被当**颜色标记** →
   `ValueError: Tag "<module>" does not correspond to any known color directive`

把内容留在**值**里就没这些问题：`format_map` 只把值当字符串替换，不再解析。
模板恒定还让 loguru 的格式缓存（`lru_cache(maxsize=64)`）一直命中。
`_json_format` 的注释里记了这两条的现场，别「优化」成直接返回 JSON 字符串。

另外两点：可调用 format **不会自动补换行**（字符串 format 会），换行要自己加；
`serialize=True` 虽是 loguru 内置的结构化输出，但它**丢弃所有自定义 record 字段**
（实测 `trace_id` 全变 `None`），所以用不了。

**第三个坑：传任何 kwarg 都会让 loguru 对消息跑 `str.format`。**
`_logger._log()` 里写着 `if args or kwargs: message.format(*args, **kwargs)` ——
`exc_info=True` 也是 kwarg，一样触发。消息里若嵌着外部服务的 JSON 原文
（`{'error': {'code': 'data_inspection_failed'}}`），`str.format` 会把 `{'error': …}`
当成替换字段 → `KeyError: "'error'"`。**它炸在 `error_policy.degrade()` 的日志上，
把要记的故障顶掉、整轮请求失败** —— 2026-10-06 就是这么烧掉一次真实问答的。

所以 `logger.py` 导出的 `logger` 是一个 `SafeLogger` 代理：`exc_info` 转成
`opt(exception=)`、其余 kwarg 转成 `bind()`，**保证不往 loguru 传 kwarg**，
30+ 个调用点一行没改。`bind` / `opt` 返回的也是代理（否则链式调用后半截又落回裸 loguru）。
**别把它"简化"回 `base_logger.patch(enrich_record)`。**

---

## 异常分级处置

四路召回、图谱、联网、重排这些环节都是「失败就降级、链路继续」。此前它们一律写成
`except Exception: 记日志 + 返回空`，**把编程错误和外部故障混为一谈**——
项目历史上就因此把一个 `NameError` 吞成了 warning，图谱那一路静默失效、只有测试断言才发现。

现在先分类、再按类处置（`app/core/error_policy.py`）：

| 分类 | 什么情况 | 怎么处置 |
|---|---|---|
| `FATAL` | 代码自身不一致：`NameError` / `UnboundLocalError` / `ImportError` / `NotImplementedError` / `AssertionError` / `SyntaxError` / `IndentationError` / `RecursionError` | **上抛**，不降级掩盖 |
| `REJECTED` | 模型服务按合规拒答：DashScope 的 `data_inspection_failed` | 降级 + error，**另有一条人话直达用户**（见下） |
| `RETRYABLE` | 暂时性外部故障：超时、连接失败、429、5xx、`ServerSelectionTimeoutError` | 降级 + warning（**Phase 2 的重试接在这里**） |
| `BLOCKED` | 重试无用且要人处理：4xx（鉴权/参数）、配置缺失 | 降级 + error（带堆栈） |
| `UNEXPECTED` | 兜底：外部依赖其它异常、数据结构不符预期 | 降级 + error（带堆栈） |

```python
except Exception as e:
    return degrade(NODE_NAME, "图谱检索", {"kg_chunks": []}, e)   # 异常 → 分类处置

if not is_neo4j_available():
    return degrade_dependency(NODE_NAME, "图谱检索", {"kg_chunks": []}, "Neo4j 不可用")
```

### 三个关键取舍

**为什么 `TypeError` / `KeyError` / `AttributeError` 不算 FATAL**
它们既能由我们写错引起，也能由外部返回的数据变形引起（SDK 改结构、接口少字段），
在降级点上分不清。所以归为 `UNEXPECTED`：**仍然降级**（保住"一路坏不影响整条链路"的设计），
但记 error 级 + 完整堆栈。**"静默"才是当初真正的问题，降级本身不是**——
`FATAL` 只留给解释器明确指向"我们代码内部矛盾"的那几类，它们与外部数据无关。

**前置检查也要打降级标记**（`degrade_dependency`）
Neo4j 没起、Milvus 连不上、集合名没配——这些是主动探测到的依赖缺失，没有异常对象可分类。
但它们**同样是功能在静默失效**：图谱那一路整段没跑、产品名对齐整段没跑，
用户只觉得"答得不好"。所以单独一个入口，照样打 `degraded` 标记。
（这是实测发现的：Neo4j 停掉后跑一次问答，那条 warning 在 `--degraded` 里查不出来。）

**这一步只分类、不做重试**
README 的路线把「故障分类重试」放在 Phase 2，这里的职责是**把类型分清、处置分明**。
重试要等有了稳定的错误分布数据再加——现在连"哪类错误出现过几次"都还统计不了，
拍脑袋定重试次数只会白等。分类里已经标好 `retryable`，接重试时直接用。

**审核拒绝为什么要单列一类（`REJECTED`）**
它也是 HTTP 400，如果只按状态码判就会被归进 `BLOCKED`，而两者对用户的含义完全不同。
更关键的是**降级在这里是错的处置**：链路会一路降下去（提取被拒 → 拿原问题去检索 →
生成节点再被拒一次），用户最后看到一顿空答案，且完全不知道原因是"被审核了"。
所以 `REJECTED` 有自己的分类、自己的日志措辞（「被模型服务内容审核拒绝」而不是「失败」），
两个 LLM 节点看到它就**短路**，把 `CONTENT_REJECTED_ANSWER` 那句话直接交给用户。

识别只在 `is_content_rejected()` 一处：优先读 `exc.body`（openai 系解析好的错误体），
取不到再退回在 `str(exc)` 里找 `data_inspection_failed`。**换模型服务商只需在这里加码。**

### 怎么知道哪个功能在悄悄失效

```bash
.venv/Scripts/python.exe -m app.core.log_query --degraded --days 7
```

所有降级（不论是异常触发还是前置检查）都带 `degraded=true` 与 `kind`，
一条命令列出「哪些路在降级、降的哪一类」。

### 泳道也会把降级标出来

`log_query --degraded` 是事后查；界面上现在**直接就能看出来**：降级返回的那几路在泳道上被标成
**暖色虚线框**（悬停提示「该节点降级返回：结果是空的（依赖不可用或调用失败），不是真取到了内容」），
收尾时标题写「**检索完成 · N 路降级**」。

**为什么需要**：降级的节点会**返回空结果、照常算完成**，所以在泳道上原本跟「正常取到内容」长得
一模一样 —— 停了 Neo4j 却看到「查询知识图谱 ✓」，用户实测就是这么被绕进去的。
既然泳道是用户唯一的进度来源，它就得说真话。

实现：`degrade()` / `degrade_dependency()` 除了记日志，还会把节点名记进 `task_utils` 的本轮进度；
进度事件与 `final` payload 都带 `degraded_list`，前端 `renderPipeline` 据此加样式。

**一个必须记住的坑**：这些进度记录（done / running / **degraded**）是**按 `session_id` 存的，
而一个会话有很多轮** —— 不清就会把上一轮的记录带到下一轮。实测踩到：上一轮 Neo4j 停着、
图谱那一路降级，这一轮 Neo4j 恢复了，泳道**仍然**标它降级。所以 `run_query_graph` 在
**新开一轮**时调 `reset_task_progress()`；**续跑不重置**（恢复那一段要接着前一段累积）。

顺带一起修的：`final` 收尾**不再拿「全量节点」兜底**（那会把压根没跑过的节点也点亮），
改用后端在 `final` 里给的真实 `done_list` —— 为此把「生成答案」的完成标记挪到了推 `final` **之前**
（否则那条进度会晚于 final 到达、而前端收到 final 就关流了）。

---

## 超时与预算

两件事：**一次调用别等太久**（超时）与**一整轮别无限跑下去**（预算）。
此前多数外部客户端压根没设超时 —— LLM / 嵌入 / 视觉走 openai SDK 的隐式 600 秒，
**MinerU 的 `requests` 更是彻底无限等待** —— 一个卡住的调用会让整轮问答一直挂着，用户只能刷新页面。

### 超时落在客户端，不在图上

**框架的节点级超时对同步节点不可用**，实测报：

```
ValueError: Node timeouts are only supported for async nodes because sync Python
execution cannot be safely cancelled in-process.
```

本项目 16 个节点全是同步的（改成 async 是大改造，不做）。所以超时落在**各客户端自己**身上 ——
好处是**每一路能各自降级**：调用超时抛错 → 走既有的 `error_policy` 分类（timeout → RETRYABLE）→
那一路返回空、链路继续，正好接上上面那节。

数值都在 `app/conf/budget_config.py`、可用 `.env` 覆盖（模板见[「配置 `.env`」](#2-配置-env)）：
LLM 120s、嵌入 60s、Milvus 10s、Neo4j 建连 5s / 事务 10s、MinIO 5 / 60s、
MinerU 建连 30s / 轮询 60s / 大文件传输 300s。

**`max_retries=0` 是刻意的**：重试策略要等错误分布数据（Phase 2 剩的那项），现在别让 SDK 偷偷重试 ——
那会让「超时」看起来时好时坏。

### 预算落在 `tracked_node`，只作用于查询图

```python
# app/core/budget.py —— 节点开始前看一眼，超了就抛 BudgetExceeded
with usage_context(session_id=..., wall_clock_budget=180, token_budget=80000) as acc:
    ...
```

- 放在 `tracked_node` 这一层，是因为**两张图的所有节点都过它**：一处生效、全覆盖，节点代码一行没改
- **只能拦在节点边界**（同步图拦不到节点内部），所以最坏会超出「一个节点」的量
- **只有查询图注入这两个值**；导入侧不注入 → `check_budget` 直接跳过（导入本来就要跑几分钟、花很多 token）
- **达到即止**（`>=` 而不是 `>`）—— 所以 `0` 等于「在第一个节点前就中止」，想让预算被静默忽略是不可能的
- 超了抛 `BudgetExceeded`，`run_query_graph` **单独认它**：推一条可读的 error
  （「本次问答已中止（本轮已耗时 X 秒，达到预算上限 Y 秒）」），
  **不打堆栈、不触发检查点降级、也不混进降级统计** —— 它是主动中止，不是故障

### 验证做到哪

- 预算：默认值（180s / 8 万）下正常问答**不被误伤**（8 次调用、427 字答案、`run_error` 为空）；
  把 wall-clock 调成 0 后**账目 +0 条**（一个模型都没调就中止了）、`run_error` 文案可读、日志只有一行 warning 没有堆栈；
  节点被拦在**执行之前**（用一个计数器验证过）
- 客户端超时：`LLM_TIMEOUT_SEC=0.001` 实测抛 `APITimeoutError`（不是挂住）
- MinerU / MinIO 的超时：**已补验** —— 用 `force=true` **重新导入一份已有文档**（不新增知识库条目），
  61 秒跑完 8 个节点（含 MinerU 的上传 PDF 与下载结果包、MinIO 的图片上传），
  **切片数重导前后都是 9**（替换而非叠加）。
  首次验证失败过一次：原因是**本机挂了 VPN**，到那台国内 CDN 的 TLS 握手被掐断 ——
  报的是 0.45 秒内的 `SSLEOFError`，与超时值无关；关掉 VPN 后一次跑通

### 顺带修的一个坑

导入失败时**放弃该线程**（删掉检查点）：否则上面「重启自动续跑」会把失败的任务捡起来重跑 ——
对确定性失败（文件损坏、参数错、节点超时）就是每次重启白烧一遍。失败已经告诉用户了
（去重记录标 `failed`，可以重新上传），留着半成品没有价值。

---

## 用户主动暂停

生成答案时前端会出现「暂停」按钮。点它，**本轮生成立刻作废**，本轮就此结束 ——
半截答案保留在屏上但压暗标「已暂停」，输入框解锁。**模型不会反问**：想怎么调整，
用户自己重新提问。

**只对流式生效**：非流式是一次同步调用，中途无处可断，按钮不会出现（`/stop` 接口本身
不区分，但非流式路径不检查取消标志）。

> **注意别把两件事混起来**：这里是**用户发起**的暂停 —— 停下就结束，不追问。
> 另一件是**图发起**的中断：产品名认不出来时中断图、弹窗给出候选与选项，
> 见[「产品确认中断」](#产品确认中断图主动中断)一节（已实现）。

此前查询服务**没有任何中断能力**：图一旦开跑就一路到 END，正在往外吐字时既停不下来、
也没法告诉模型「等一下」。全仓既没有 `cancel` / `abort` / `threading.Event`，
`sse_generator` 里的 `is_disconnected` 也只是停止推送、图照跑。

```bash
# /query 的响应里多了一个 run_id（本轮标识，同时就是日志 trace）
POST /query        →  {"session_id": "...", "run_id": "974ef95c9fbc4e50"}

# 点暂停：带上 run_id，只认当前在跑的这一轮
POST /query/{session_id}/stop   body: {"run_id": "974ef95c9fbc4e50"}
                                →  {"stopped": true}    # false = 这一轮已经结束了，信号作废
```

被打断时推给前端的是一个新事件 `paused`（不是 `final`）：

```jsonc
{ "done_list": ["确认问题产品", "切片搜索", ...] }   // 真实进度，供前端渲染泳道
```

相关代码：取消标志在 `app/utils/task_utils.py`，流式循环的打断点在
`app/query_process/agent/nodes/node_answer_output.py`（`_generate`），
`paused` 事件由 `query_service.run_query_graph` 在收尾时推，前端在 `chat.html` 的
`requestPause` 与 `paused` 监听。

### 三个设计取舍

**为什么要一个 `run_id`，而不是只按 `session_id` 置标志**
前端的 `sessionId` 是**跨轮复用**的，而暂停信号来自另一个 HTTP 请求——用户点慢了、
或者网络延迟，上一轮的暂停就可能在下一轮开跑之后才打到。只按 session 置位会**误杀下一轮**。
所以每轮发一个 `run_id`，`request_stop` 只认与当前登记一致的那一轮。
这个 id 不必新造：`usage_context(trace_id=...)` 本来就支持外部注入，于是 run_id =
日志 trace = 暂停令牌，一个 id 三用。

**为什么用进程级 dict，而不是归因那套 ContextVar**
`request_context` 的 ContextVar 在 LangGraph `copy_context()` 到子线程后，**子线程内的修改
不回流父上下文**；何况暂停信号隔着另一个 HTTP 请求，更读不到。所以仿 `task_utils` 的既有风格
用模块级 dict。（代价：**单进程前提**，多 worker 部署会失效——与「`task_utils` 外移」是同一笔债。）

**为什么这件事不需要 checkpointer**
checkpointer 的价值是保住图内中间状态，好让恢复时不必重算。而这里的语义是
「作废重来」——用户回答后是**全新一次运行**，中间结果一个都不复用，检查点就没东西可救。
（需要 checkpointer 的是另一种场景：**图主动中断**去求用户确认，那要保住已经跑完的检索结果。
两者是两条独立的线 —— 后者见[「产品确认中断」](#产品确认中断图主动中断)。）

### 两个坑

**被打断的那次生成，账本上会留下一条「用量为空」的记录**
`stream_usage` 的用量在**最后一帧**才带回来，中途 `break` 就永远等不到——实测这一笔记成
`tokens=0+0, cost=None`，在报表与前端消耗条上显示为**「未计价」**。
所以：**已有内容照样计费**（token 已经产出了），但**这一笔的成本记不上**，
暂停轮的账面会偏低（实测完整问答约 0.0095 元，暂停轮约 0.005 元）。
它不是静默丢失——「未计价」那个数字就是它的痕迹。

**被暂停的节点不能算「已完成」**
`node_answer_output` 的 `finally` 里原本无条件 `add_done_task`，打断后仍会标成
「生成答案 ✓」，泳道谎报完成。现在 `cancelled` 时不标；前端也**不能沿用 `final` 那套收尾**
（这一轮没有 `final`：光标、消耗圆点、泳道展开态、标题、hint 都得在 `paused` 处理器里逐项补）。
另外 `run_query_graph` 在暂停时**刻意不推 progress**——前端收到 `paused` 就关掉 SSE 连接了，
再推只会刷「No queue found」告警。

### 怎么验证

前端：起查询服务，问一个会生成长答案的问题，生成途中点暂停，逐项确认
「半截答案压暗并标『未完成』/ 输入框解锁 / 没有出现任何提问卡片 /
泳道没有把『生成答案』点亮」。

```bash
# 离线（秒级，不调接口）：流式边界 4 例 + 暂停中断 2 例
.venv/Scripts/python.exe -m app.query_process.agent.nodes.node_answer_output
# 图结构（应仍是 12 个节点，生成之后直接到 END）
.venv/Scripts/python.exe -m app.query_process.agent.main_graph
```

---

## 图检查点（checkpointer）

Phase 2 的第一项，也是**「图因缺信息主动中断、等用户回答后从断点继续」的前置能力** ——
`interrupt()` 离了 checkpointer 根本不生效。**两张图都接了**：查询图（配合产品确认中断）
与导入图（配合下面的「重启自动续跑」）。

存储用 `MongoDBSaver`（`langgraph-checkpoint-mongodb`）：复用现有的 mongo 容器与
`MONGO_DB_NAME`，不新增服务；它自带 TTL，省得自己写清理。

```python
from app.clients.mongo_checkpoint_utils import graph_config
from app.query_process.agent.main_graph import get_query_app

# 接上检查点后，每次 invoke 都必须带 thread_id，否则 LangGraph 直接报错
get_query_app().invoke(init_state, graph_config(run_id))   # thread_id = 本轮 run_id
```

### 三个设计取舍

**`thread_id` 就用现成的 `run_id`（一轮一问一个 thread）**
前端的 `session_id` 是跨轮复用的；若拿它当 `thread_id`，上一轮的 `answer` / `reranked_docs`
会被下一轮**从检查点里带出来**，多轮之间就串味了。`run_id` 本来就一轮一个（同时是日志 trace
与暂停令牌），直接复用，不必新造 id。代价是不复用历史 —— 但对话上下文本来就存在 Mongo 里，
不靠图状态。

**`main_graph` 改成惰性编译（`get_query_app()`）**
checkpointer 是**编译时绑定**的。Mongo 不可用时我们要降级成内存 saver，等它恢复了得换回真
saver —— 而模块级的 `query_app = builder.compile()` 是导入即编译、换不了。所以改成按 saver 的
kind 缓存并重编译（重编译本身很便宜）。`query_app` 这个名字随之消失，4 处调用点都改成
`get_query_app()`。

**为什么要降级，而不是硬失败**
checkpointer 在**关键路径**上（每个节点写完就写一次，一次问答 8 次左右），Mongo 一抖服务就
整个不能问答，代价太大。所以连接失败快失败（`serverSelectionTimeoutMS=2000`）+ 60 秒熔断，
期间退回 `InMemorySaver`，服务照常问答。**代价说清楚**：那段时间没有持久化 ——
中断/续跑不可用、进程重启即丢。这套快失败 + 熔断照抄的 `mongo_usage_utils`。

### 导入侧：重启自动续跑

导入才是 checkpointer 收益最大的地方 —— MinerU 要跑几分钟，服务一重启整轮白费。
所以导入图除了接上检查点，还带了**自动续跑**：

```jsonc
// thread_id 约定：import_<task_id>（task_id 是上传时生成的 uuid）
上传 → 后台跑图（stream + graph_config(import_<task_id>)
重启 → resume_pending_imports() 扫 checkpoints 里 import_ 前缀的线程
     → get_state().next 非空 = 没跑完 → 后台线程里 stream(None, 同一个 thread) 接着跑
```

**判据与机制**：`next` 非空即「跑到一半没了」；`stream(None, config)` 会**从最后一个完成的节点
继续，已完成的不重跑**（实测：3 节点探针图，B 抛错后再 invoke 只跑了 B）。

**为此补的两个 state 字段**：`file_hash` / `tenant_id` —— 续跑时要从检查点读回它们才能收尾
（回填去重记录、让恢复那一段的账目有租户）。**必须在 `state.py` 里声明**，否则被静默丢弃。
另外收尾用的最终状态改成**从检查点读**（`get_state().values`）而不是靠 stream 事件拼 ——
续跑时前面几个节点不再执行，靠事件会把切片数报成 0。

**代价（认了）**：

- 续跑会**重跑「被杀那一刻正在执行的那个节点」**：杀在 MinerU 阶段 = 重烧一次解析配额
- **单进程前提**：两个 worker 会各自续跑一遍（与 `task_utils` 从内存外移是同一笔债）
- 重启后原来的 SSE 连接没了，续跑期间的进度事件没有队列可推（日志会有 `No queue found` 噪音）；
  重新打开导入页能看到新进度

**实测（完整验证，含杀进程）**：

- 正常导入 H3C LA2608（171K）：63 秒跑完 8 个节点、9 条切片入库、去重记录 `completed`
- 导入 Aolynk CB304n（780K），**等到「嵌入完成」后杀掉服务进程** → 检查点停在
  `next=('node_import_kg',)`、去重记录仍是 `processing` → **重启后自动认出并只跑了 `node_import_kg`**，
  最终 **Milvus 仍是 62 条切片（无重复）**、去重记录翻成 `completed`、图谱 62 Chunk + 287 Entity
- 反例：没有未完成任务时重启 → 日志只打「没有未完成的导入」，不做任何事

### 四个坑

**state 里不能放 Mongo 文档**
state 会**整份**进检查点走 msgpack，而 `bson.ObjectId` **不是可序列化类型** —— 一旦放进去，
整轮问答直接报 `Type is not msgpack serializable: ObjectId`。踩过：`node_item_name_confirm`
早先把 history（`get_recent_messages` 返回的每条都带 `_id`）写进 state，于是会话一有历史就炸。
**最阴的是它只在有历史时复现**，全新会话反而一切正常，拿新会话测根本发现不了。
规矩：往下游传**纯标量**；Mongo 文档要么别放（下游自己回库里现读），要么当场转成字符串。

**checkpoint 存的是整份 state**
里面带着四路召回回来的切片正文 —— 实测一次完整问答写 **9 条检查点 + 37 条写记录**。
所以 TTL 不是可有可无的：7 天，建在 `checkpoints` 的 `created_at` 上。
**已经存在的索引不会再改**，所以 TTL 必须第一次建集合时就带上。

**Mongo 中途挂掉，那一次问答会失败**（降级救不了）
降级只在「建 saver 时」判断。跑到一半写不进检查点，那一轮就报错结束，而不是降级。
补救是 `note_checkpointer_failure()`：它让**下一次**运行改用内存 saver —— 否则每一轮都撞
同一堵墙直到进程重启。要做到「中途也不失败」得自己包一层 saver，目前没做。

**每个节点多了一次 Mongo 写**
这是新引入的耦合：问答延迟现在也受 Mongo 写性能影响。排查变慢时先看这里。

### 怎么验证

Mongo 里会多出两个集合：`checkpoints` 与 `checkpoint_writes`（DB 用 `MONGO_DB_NAME`）。
实测结论：两个不同 `thread_id` 的产品名集合**无交集**（不串味）；每个 thread 最早的状态是空的
（全新线程从零开始）；从检查点里能读回 `answer` 与 7 条 `reranked_docs`
（这正是下一步 `interrupt`/resume 要用的能力）。

降级路径**不动容器就能验证** —— 把 Mongo 指到一个不存在的端口：

```bash
MONGO_URL=mongodb://127.0.0.1:29999 PYTHONPATH=. .venv/Scripts/python.exe -c \
  "from app.clients.mongo_checkpoint_utils import get_checkpointer; print(get_checkpointer())"
```

实测：首次 2.7 秒（撞一次选节点超时），之后 0.0000 秒（熔断期内不再重试），返回内存 saver。

---

## 产品确认中断（图主动中断）

图跑到「认不出是哪个产品」时**主动中断**，前端在输入框上方弹一张卡片：候选（含近似候选，
附来源文档与匹配度）+ 一个自由输入框。用户选完，**从同一个 thread 恢复**，接着检索出答案。

```jsonc
// SSE 事件 confirm（不是 final）
{ "question": "「烫金机盒怎么安装？」没能锁定到具体产品，你想问的是下面哪一个？",
  "options": [ { "item_name": "Brother HAK 180 烫金机", "file_title": "hak180使用说明书",
                 "score": 0.578, "near": true } ],
  "allow_custom": true,
  "done_list": ["确认问题产品"] }        // 真实进度：四路检索还没跑

// 恢复（run_id 就是那一轮的 thread_id）
POST /query/{session_id}/resume
     { "run_id": "...", "choice": "Brother HAK 180 烫金机", "is_stream": true }
  → 409 如果这一轮已经不在等待（已结束 / run_id 不对 / 不属于这个会话）
```

四路检索、重排、生成那一整段都在**恢复之后**才跑 —— 用户在卡片上选一次就够，
不必重新提问一遍。

**这是「图发起」的中断，别和「用户主动暂停」混起来**：暂停是用户按下按钮、停下就结束、不追问；
这里是图认不出产品、必须问到才继续。两者共用 checkpointer，但是两套触发。

### 四个设计取舍

**中断必须独立成节点（`node_ask_user`）**
`interrupt()` 所在的节点**恢复时会从头重跑**（实测：同一节点被执行两次）。所以
`node_item_name_confirm` —— 它有 Mongo 写入、有模型调用、有记账 —— **绝不能**放 `interrupt()`；
它只把「要不要问、问什么、有哪些选项」写进 state，由轻量的 `node_ask_user` 去中断。
同理，`node_ask_user` 里 `interrupt()` 之前那段必须**无副作用**。

**「选了会怎样」用真实数据，不调模型**
`kb_item_names` 里就有 `file_title`（这个产品名出自哪份手册），直接显示
「用《hak180使用说明书》回答」，再配上匹配度。比让模型编一句影响说明更准、更省钱。

**`thread_id` 仍是「一轮一个 `run_id`」**
恢复必须打到**同一个 thread**，所以这个 id 在两段之间不能变；而它同时还是日志 trace
与暂停令牌 —— 一个 id 三用，前两个功能都不用改。

**恢复前必须校验三件事**
`get_state` 看 `next` 里有 `node_ask_user`、确实带着中断、且 checkpoint 里的 `session_id`
与请求路径里的一致。**少了最后一条，凭一个 run_id 就能跨会话恢复别人的上下文**。
不合法就 409，而不是让 LangGraph 拿空 state 起跑（那样只会抛一个莫名其妙的 `KeyError`）。

### 三个坑

**中断不能掉进「完成」分支**
`run_query_graph` 收尾时必须先判 `__interrupt__` —— 否则会推一条 `completed`，泳道谎报
「已完成」，前端也就不会去弹卡片。（和「暂停」那次是同一类 bug，两条路都得单独判。）

**同一会话先后两次订阅，队列不能互相删**
恢复要**复用同一个 SSE 队列**（挂起时前端那条连接还开着）。`create_sse_queue` 是覆盖写，
而旧生成器晚到的 `finally` 又会把新队列删掉 —— 两头都会让恢复后的事件全丢。
所以 `remove_sse_queue` 改成「只删自己那一个」，恢复端点也改成「队列存在就复用、不存在才建」。

**用量会跨段归零**
两段各自 new 一个累计器，而消耗条写的是绝对值 → 恢复后会从 0 重新跳，看着像钱退回来了。
前端在提交时把挂起前的累计存成基数加上去（`addUsage`）。

### 怎么验证

```bash
# 离线：迷你图 + 真检查点走一遍 interrupt → resume（打桩，不调模型接口）
.venv/Scripts/python.exe -m app.query_process.agent.nodes.node_ask_user
# 图结构与三分支路由（need_confirm / 预置 answer / 正常检索）
.venv/Scripts/python.exe -m app.query_process.agent.main_graph
```

浏览器实测过：问「烫金机盒怎么安装？」→ 出卡片（3 个近似候选，各带来源文档与匹配度）→
选第一个 → 恢复后给出 403 字答案 + 4 张配图，消耗条定格在 **¥0.0108 / 9 次调用**
（不是只显示恢复段那半截，说明基数叠对了）；卡片在时直接问别的 → 旧卡片撤掉、新问题照常跑完；
拿已结束的 run_id 恢复 → **409** 且文案清楚。非流式也实测过：`/query` 的响应里带
`need_confirm` 与 `clarify`（选项里的 `score` 是原生 `float` —— 它会进检查点走 msgpack 序列化，
numpy 标量会直接报错，所以取数时就转了）。

---

## 端口一览

| 端口 | 服务 |
|---|---|
| 7474 / 7687 | Neo4j Browser / Bolt |
| 8000 | Attu（Milvus 图形界面） |
| 8001 | 文档导入服务 |
| 8002 | 知识库查询服务 |
| 8081 | mongo-express（MongoDB 图形界面） |
| 9000 / 9001 | MinIO API / 控制台 |
| 19530 | Milvus |
| 27017 | MongoDB |

---

## 数据模型

**`kb_chunks`**（文档切片）

| 字段 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | INT64 | 主键，自增 |
| `content` / `title` / `parent_title` | VARCHAR | 切片正文与层级 |
| `part` | INT8 | 长段落二次切分的序号 |
| `file_title` | VARCHAR | 源文档名，**幂等清理依据** |
| `item_name` | VARCHAR | 所属产品主体 |
| `dense_vector` | FLOAT_VECTOR(1536) | 稠密向量，HNSW + COSINE |

**`kb_item_names`**（产品主体）

| 字段 | 类型 | 说明 |
|---|---|---|
| `item_name` | VARCHAR(512) | 主键 |
| `file_title` | VARCHAR | 来源文档 |
| `dense_vector` | FLOAT_VECTOR(1536) | 产品名向量 |

**MongoDB**

| 集合 | 用途 |
|---|---|
| `chat_message` | 会话历史（有 `session_id` + `ts` 复合索引；但**取历史按 `_id` 排序**，见下） |
| `imported_documents` | 上传去重指纹（`file_hash` 唯一索引） |
| `llm_usage` | 调用账本，每次模型调用一条（append-only，`ts` / `trace_id` / `tenant_id` 索引），见[「调用记账」](#调用记账) |
| `users` | 访问密钥（只存 sha256）与租户标识，见[「访问鉴权」](#访问鉴权) |

**会话历史的排序键是 `_id`，不是 `ts`**（2026-10-06 改，详见 HANDOFF §3.15/§4.18）。
一句话理由：`ts` 在这里**不可信** —— 本机 `datetime.now().timestamp()` 连取 2000 次只有
2 个不同值，同一轮连续写入的两条消息经常撞出**完全相同**的 ts；而且它历史上还被更新刷掉过
（`save_chat_message` 回填改写问题时连 `ts` 一起 `$set`）。结果是 14 个真实会话里 9 个的
历史顺序是反的（助手那句澄清问题排在用户提问前面）。
ObjectId 在同一进程内单调递增，**就是真实插入顺序**，而插入顺序在本项目里等于对话顺序。
换成 `_id` 之后 14 个会话全部读对 —— **包括 `ts` 已经被写坏的历史数据，不需要迁移**。
（若只是把 `_id` 加作次级键、仍以 `ts` 为主，救不了这些数据：它们的 ts 是**真的**偏后，不是撞车。）

**文档字段**：`session_id` / `role` / `text` / `rewritten_query` / `item_names` /
`images`（`[{"url", "caption"}]`，答案配图**连图注一起存**，否则重新打开历史时每张图都会
退化成「未标注来源」）/ `ts`（仅供参考，排序不用它）+ Mongo 自带的 `_id`。
**注意 `text` 和切片的 `content` 不是一回事**（见 HANDOFF §4.19）。


**Neo4j**（知识图谱，按 `file_title` 隔离）

| 元素 | 说明 |
|---|---|
| `(:Entity {name, type, item_name, file_title})` | 抽取出的实体，type ∈ 部件 / 操作 / 故障 / 参数 / 其他 |
| `(:Chunk {chunk_id, title, parent_title, item_name, file_title})` | 切片节点，`chunk_id` 对应 Milvus 主键 |
| `(:Entity)-[:APPEARS_IN]->(:Chunk)` | 实体挂载到来源切片 |
| `(:Entity)-[:REL {type, file_title}]->(:Entity)` | 实体间关系，type ∈ 组成 / 导致 / 解决 / 使用 / 连接 / 参数属于 / 其他 |

---

## 设计取舍

**去除稀疏向量**
项目原设计用 BGE-M3 生成稠密+稀疏双向量做混合检索。现改用 DashScope `text-embedding-v2`，**只输出稠密向量**，因此：
- `kb_chunks` / `kb_item_names` 均无 `sparse_vector` 字段
- 检索走 `dense_search` 单路，不再有 `hybrid_search`
- 代价：失去关键词精确匹配能力（型号、参数名这类查询受影响）

**产品名用向量对齐，而非字符串匹配**
用户可能说「HAK180」而库里存的是「Brother HAK 180 烫金机」，字符串匹配无法处理。改用向量相似度分档：
- `≥0.85` 自动确认
- `0.6 ~ 0.85` 反问用户选择
- `<0.6` 判为未找到

**文档身份用「文件内容哈希」**
同一份文件改名后仍应识别为重复，因此去重依据是 SHA-256 而不是文件名。

**幂等策略**
- `kb_chunks`：按 `file_title` 先删后插（**不能按 `item_name`**，不同文档可能描述同一产品，会误删）
- `kb_item_names`：`item_name` 作主键 + 按 `file_title` 清理同文档旧名

**MCP 联网搜索：沿用教程的异步 SDK，但传输类必须换、`mcp` 必须钉在 1.x**
整体按教程写（`openai-agents` 的 MCP 客户端 + `asyncio` 桥接），但有三处不得不偏离：

- **传输类换成 `MCPServerStreamableHttp`**：教程的 `MCPServerSse` 连 `/sse`，而本服务的 `/sse` 返回 200 后**一个字节都不推**（实测挂起 25 秒无输出），该客户端依赖服务端先发 `endpoint` 事件，根本用不了。改连 `/mcp`。
- **`mcp` 必须 `<2`**：mcp 2.x 改用 `server/discover` 新握手（协议 `2026-07-28`），百炼服务端仍是 `2024-11-05` 老协议，收到直接回 **HTTP 500**。**升级 mcp 会静默打断联网搜索**，`pyproject.toml` 已加约束。
- **工具名与参数**：本服务只有 `search_pro`，且**只接受 `query`**；照教程传 `count` 会直接 `isError`。

还有一处教程没覆盖的坑：非流式路径下 `run_query_graph` 是在 `async def` 路由里**直接被调用**的，此时事件循环已在运行，`asyncio.run()` 会抛 `RuntimeError`。`mcp_search_utils._run_coro` 检测到这种情况就另开线程执行（流式路径走 `BackgroundTasks` 线程池，不受影响）。

**RRF 只融合切片类召回，联网结果留给重排**
联网搜索返回的是 `{title, url, snippet}`，**没有 `chunk_id`**，而 RRF 靠 `chunk_id` 跨路去重计分，硬塞进去只会被当成无效项丢弃。教程的设计正是如此分工：RRF 管同源融合（基线 / HyDE / 图谱，都是 Milvus 切片），跨源合并（切片 + 网页结果）交给 `node_rerank`。所以 `node_web_search_mcp` 的结果不会白做，它在重排阶段并入。

**重排走 API，动态 Top-K 的阈值按 API 的分数尺度改过**
教程用本地 BGE（`FlagEmbedding` 的 `FlagReranker`），本项目改用 DashScope `gte-rerank-v2`，取舍同嵌入模型——项目已是全 DashScope 架构（LLM / 嵌入 / 重排共用一个 key），本地路线要装 torch（约 2GB）+ 下载 1.3GB 模型，且本机无 CUDA 只能 CPU 推理，而重排每次查询都要跑。

**两者的分数尺度不同，教程的阈值不能直接搬**：

- 本地 BGE 返回**无界 logits**，教程的断崖阈值是按这个尺度调的
- `gte-rerank-v2` 返回 **0~1 归一化分数**，实测真实查询下相邻最大落差仅约 0.14

据此在 `node_rerank.py` 顶部改了两处常量：

- **去掉绝对阈值 `GAP_ABS=0.5`** —— 在这个尺度下永远不会触发，是死参数，只保留相对阈值 `GAP_RATIO=0.25`
- **`MIN_TOPK` 由 1 抬到 3** —— 截断循环从 `MIN_TOPK-1` 起探测，`MIN_TOPK` 因此是硬地板；教程的 1 曾导致 5 条候选因 0.79 → 0.38 的陡降被截到只剩 1 条，答案生成靠一条切片支撑显然不够

调参时要先用真实查询统计分数分布，不要凭感觉改。

**最终 Top-K 给本地切片留配额（`LOCAL_MIN_SLOTS` / `LOCAL_MIN_SCORE`）**
截断只看分数，而重排模型对「自然语言提问 vs 新闻散文」天然给高分、对说明书那种短促带图注的
切片给低分。实测「Brother HAK 180 烫金机长什么样」：联网新闻 0.8886~0.6760，本地那条
**「HAK 180设备外观示意图」只有 0.3384** —— 本地 0 条进最终结果，答案 100% 由百度新闻拼出来，
**而知识库里明明有那张图**。这不是阈值卡掉的，放宽截断也轮不到（差近 2 倍）。

所以配**硬配额**而不是给本地加权：「这个领域里知识库是权威、联网只是补充」该表达成
**保证留位**，而不是稍微加点分——靠加分翻不过 2 倍的差距。
门槛 `LOCAL_MIN_SCORE=0.25` 是**照实测分定的**：该救的（外观图 0.3384、产品简介 0.8090）
在它之上，该挡的（0.1808 / 0.1605 / 0.1227 / 0.0059）在它之下。
「打印质量不清晰怎么办？」本地最佳只有 0.1808（说明书确实没对应内容），那种情况
**留给联网是对的** —— 配额只保「够线」的，不会硬塞。

**图谱可视化首次引入外部 CDN，并用三重措施兜住**
项目前端此前是**零外部依赖**（所有脚本样式内联在单个 HTML 里，无静态目录）。图谱渲染需要图库，权衡后选了 ECharts（力导向布局效果好、中文文档全、代码量最小），代价是打破了这个惯例。

三条措施降低风险：

- **三个 CDN 源按序回退**，且都是在本机实测过可完整下载的（npmmirror 0.66s → bootcdn 8.2s → jsdelivr 13.6s；`staticfile` 实测连接被断故未收录）
- **懒加载**：只在首次点「图谱」时才拉取，`Promise` 缓存避免重复请求
- **文本降级**：三个源都失败时（本机环境有 Clash 拦 CDN 的先例）不白屏、不抛未捕获异常，改为展示类型分布 + 关联最多的 Top20 实体，信息照样能看

另外两点渲染取舍：**切片不作为节点画进图**（84 个切片会把 257 个实体淹没），改为悬停实体时用 tooltip 告知它出现在几个切片里；**标签默认不显示**，只在悬停与缩放 ≥1.5 倍时出现，否则 257 个中文标签必然糊成一团。

**答案的 Markdown 在前端手写渲染，不引库**
模型会输出 `**粗体**`、有序/无序列表、嵌套子步骤、`>` 引用块。原先前端用 `textContent` 直接显示，这些标记原样露出、观感很差。

没有引 marked.js 之类的库，而是写了个约 100 行的渲染器（`chat.html` 的 `renderMarkdown`）。覆盖：标题、代码块、粗体/斜体/行内代码、链接、有序与无序列表（含一层嵌套）、引用块、分隔线、段落——比模型眼下实际用的多一些，留了余量。

两个安全点：

- **先把整段 HTML 转义再解析**。答案里夹着文档正文，不转义等于把切片内容当标签执行（已实测 `<img onerror>` 与 `<script>` 都被转义成文本、不触发）
- **链接只放行 `http(s)://` 与本域路径**，挡掉 `javascript:` 之类的伪协议；外链带 `rel="noopener"`

流式过程中**每收到一块就整段重渲染**，而不是追加文本节点：Markdown 上下文相关，列表要凑齐才能成块，追加渲染会先冒出一堆裸标记。实测流式期间加粗就已生效，不会等 final 才「跳变」。

**答案的配图单列成图卡，且只放行参考内容里真实出现过的链接**
`prompts/answer_out.prompt` 要求模型在答案末尾追加一个【图片】区块。`node_answer_output` 把它拆出来单独作为 `images` 返回、并从正文里去掉——用户不必看到一堆裸链接。

每个元素是 `{url, caption}`。**caption 优先取该图的 alt 文本**——导入时 `node_md_img` 已经让多模态模型逐张看过图，把描述写进了 Markdown 的 alt 里（如「打开烫金膜盒支架盖，按箭头方向将烫金膜盒插入支架」）。alt 为空或是占位（`图片`）时才退回用章节标题。

**为什么不直接用章节标题**：同一个章节下常有多张不同的图，用标题会让它们图注一模一样——实测 5 张图的图注只有 2 种，其中一种还是「向下轻推烫金膜盒，直到它锁定到位。」这种对图本身毫无说明的句子。改取 alt 后，5 张图得到 5 条各自准确、互不相同的描述。

前端据此渲染成「图 N + 名称」的图卡（带边框、点击看原图），并用一条分隔线与正文隔开，避免几张裸图突然堆在末尾。

**为什么要白名单过滤**：模型可能编造或改写链接，直接透传会让前端显示一排破图。上面那张「url → 图注」映射正好充当白名单，不在其中的一律丢弃并记 warning——**一个结构同时解决图注与过滤两件事**。

流式模式下**图片区块不推给前端**：一旦读到 `【图片】` 标记就停止推送 delta（但仍继续累积原文，否则解析不出链接），所以打字机效果不会闪过一段裸 URL。

**标记可能被切在两个 chunk 之间**（先到「【」、下一块才是「图片】」），所以推之前会先扣住
`len("【图片】")-1` 个字符不推——它们随时可能是标记的前缀。这条曾经漏掉：那个孤零零的
「【」会跟着打字机闪过去，直到 final 覆盖才消失。修完后不变量是
**「推出去的正文 == 最终答案里图片区块之前的部分」**，四个边界场景（切块 / 无标记 / 整块 /
流停在半个标记上）都在 `node_answer_output._check_stream_boundary()` 里离线跑，不调接口。

**一片都没检索到时直接兜底，不调 LLM**
`reranked_docs` 为空时返回固定的「没有找到相关内容」而不是让模型自由发挥——没有参考内容时它只会编。

**图谱检索只回 chunk_id，正文回 Milvus 取**
图谱的 `:Chunk` 节点只存 chunk_id 与标题、**不存正文**——正文留在 Milvus。这样图谱不必重复存一份文本，代价是查询时多一次 Milvus 批量取（用的正是 `fetch_chunks_by_chunk_ids`，它本来就是为「只有 chunk_id 没有文本」的场景准备的）。

查询策略是「**种子 + 一跳邻居**」：问题里直接出现的实体名作种子（权重 2），种子的一跳邻居作扩展（权重 1），再按实体 df 打折。一跳扩展正是图相对纯向量检索的价值——能捞到语义上并不相似、但通过关系关联的切片。

值得留意的是，打分里的 df 取自图谱，反映的是**抽取覆盖度**而非真实词频，所以它作为「特异性」信号并不可靠。试过 `1/√df` 与 `1/df`，都无法让排序完全精确。但在当前架构下这可以接受：**RRF 只按排名融合**（rank 1 与 rank 2 的贡献差不到千分之一），真正的精度由下游重排按正文语义决定，图谱这一路的价值在于**把正确切片捞进候选集**——实测确实做到了。

**知识图谱按文档隔离，不做跨文档实体合并**
每个 `Entity` / `Chunk` 都带 `file_title`，同一实体出现在两篇文档里就是两个节点。这牺牲了跨文档的实体归并，换来的是**清理简单且安全**——一条 `MATCH (n) WHERE n.file_title = $ft DETACH DELETE n` 就够，不会误删其他文档。这和 Milvus 幂等清理只按 `file_title` 是同一条原则。

代价是硬约束：**不存在跨文档共享节点**。将来若真要做实体合并，这套清理会立刻失效，届时要改成按文档记录拥有关系再删。

另外两点设计：

- **关系类型是 `:REL` 上的属性、不是关系标签**。Cypher 无法参数化关系标签，拼字符串既有注入风险又无法约束取值，所以统一用单标签 + `type` 属性，取值在 Python 侧按白名单兜底成「其他」
- **不建 `:Product` 节点**。每篇文档只有一个 `item_name`，做成实体属性即可；「按产品找文档」已由 Milvus `kb_item_names` 承担，图谱不重复存一份

**图谱抽取必须「先抽取、再清理、最后写入」**
Milvus 每次重新入库都会生成**全新的 chunk_id**，重复导入时旧 `:Chunk` 节点带的是失效 id、`APPEARS_IN` 会变成指向幽灵切片的悬空边，所以写前必须清理。但顺序不能颠倒：先清后抽的话，一次抽取失败就把已有图谱清空了，比残留更糟。

还有个坑值得记：LLM 会在 `relations` 里引用没登记进 `entities` 的名称（例如把「装入烫金膜盒」当 src 却不在实体列表里），而 Cypher 的 `MATCH` 找不到端点会**静默丢边**。除了在提示词里明确要求「被关系引用的名称必须先在 entities 里登记」，代码侧还会把缺失端点补登记为实体（type 记「其他」），并统计补的数量——补得过多说明提示词在漂移。

---

## 回归用例

一条命令跑完「那几个曾经坏过的不变量」：

```bash
.venv/Scripts/python.exe -m app.core.regression
.venv/Scripts/python.exe -m app.core.regression --verbose   # 连日志一起看（默认静音）
```

**为什么要有它**：2026-10-06 一天修掉 8 个 bug，**全是静默的** —— 不报错、不崩溃、界面也不变样，
只是功能悄悄失效（本地检索归零、有图变没图、多轮上下文为空……）。所以它们**一个都不是测试发现的**。
用例只为一件事：**别让已经修好的问题悄悄复活**。它**发现不了新问题**。

**11 条**：那 8 个 bug + 更早修过的两个（流式图片标记边界、中断→恢复）+ 会话列表。
冷启动 **11~16 秒**，其中 10 秒以上是**一次性 import 开销**，其余 9 条加起来不到 1 秒；
**11 条里只有 1 条碰付费 API**（一次 embedding，约 ¥0.00001）。

三个刻意的设计：

- **不引 pytest、不建 `tests/`** —— 顺着项目既有约定 `_check_xxx() -> list[str]`，
  用例**贴在它所守护的代码旁边**（`node_rrf._check_fusion`、`mongo_history_utils._check_history_order` …），
  改那个文件的人立刻看得到
- **显式列出清单，不自动发现** —— 「这几个曾经坏过」这件事本身就是清单的意义
- **依赖不可用标 `SKIP` 而非 `FAIL`，退出码仍是 0**（只有 `FAIL` 才返回 1）——
  否则 Docker 抖一下看起来就像回归了，这网很快就会没人信；默认静音日志，`--verbose` 可看

**这张网是验过的**：把那 8 处修复**逐个破坏掉**，**8/8 对应用例都变红**（mutation check）。
第一次跑只抓到 6/8，暴露了两个缺口并补齐（详见 HANDOFF §3.19）——
这件事值得单独强调：**没做 mutation check 的回归网，能不能兜住只是猜的。**

它同时是[「评估路线」](#评估路线待办与进度)的**第 1 批用例**：评估那条线要做的事，
第一步就是把这批「曾经坏过的不变量」固化下来。区别在于评估还会往上加
Gold Recall@K、接地性这些**质量**指标，而这一节只兜「已经坏过的东西别再坏」。

---

## 已知问题 / 待办

| 项 | 说明 |
|---|---|
| 图谱侧排序只是近似 | `node_query_kg` 按「种子实体权重 2 / 一跳邻居 1，再除以 df」打分，但 **df 反映的是抽取覆盖度而非真实词频**（`HAK 180` 只被抽到 2 个切片里），所以排序不精确。好在 RRF 只看排名、精度由下游重排决定——图内实测正确的切片都进了候选集 |
| ~~RRF 没按 chunk_id 去干净重~~ | **✅ 已修**（2026-10-06）：两路返回的 `chunk_id` 类型不同（向量 `int` / 图谱 `str`），而 `score_map` 按它去重 —— 同一个切片会融合出 2 条，既重复占位，也让「多路命中累加得分」这条 RRF 核心语义失效。现在当键之前统一转成 `str`，**输出的实体保留原生类型**。真实数据：5+5 输入 → 9 条、无重复 |
| ~~会话不持久~~ | **✅ 已解决**（2026-10-06）：`sessionId` 存 localStorage、启动拉 `/history`、配图连图注一起存 —— 刷新后能接着聊，历史与图片都还原。剩下三条边界见下 |
| 刷新时正卡在确认卡片上的那一轮恢复不了 | 卡片内容与 `run_id` 都不落库，后端也没有「按 session 找回挂起 thread」的接口。刷新后那一轮只剩一条无法续接的澄清问句文本，用户只能重新提问 |
| 窄屏（≤620px）隐藏左侧会话栏 | 优先保阅读宽度。代价是手机上没法切换/新建会话 —— 要的话得改成抽屉式 |
| 删除会话不清检查点 | 检查点按 `run_id`（=thread_id）存，与 `session_id` 没有索引关联，删不干净；靠 7 天 TTL 回收 |
| 自带图测试场景1恒失败 | `main_graph.py` 的 `__main__` 拿「烫金膜盒怎么安装？」（不带型号）当查询，产品名确认必然判拒识，四路检索全被跳过。这是既有缺陷，与该测试想验证的图拓扑无关；换成完整产品名即可通过 |
| 相似度阈值 | `kb_item_names` 的 0.85/0.6 阈值取自教程代码（教程正文写的是 0.95，两处不一致） |
| 无引用的模块 | `format_utils.py`、`mongo_history_utils_new.py` 均无引用（后者还是 `sort("ts")` 的老写法，别拿它当模板） |
| 重排相对阈值验证样本少 | `GAP_RATIO=0.25` 由教程继承（相对值可跨尺度迁移），但只在少数真实查询上验证过，候选规模变化后可能仍需微调 |
| 计价表是快照 | `pricing_config.py` 里的单价取自百炼 2026-10 的价目表；阶梯计价只按最低档算。tokens 是原始事实，单价更新后报表会自动按新价重算，但**表本身要人工跟** |
| 账本有两处不计成本 | 联网搜索按次计费、单价未公开（账本记次数、成本标为「未计价」）；MinerU 按页数配额计费、与 token 无关，不在账本内 |
| 暂停轮的生成调用记不上用量 | 流式用量在**最后一帧**才返回，中途打断就拿不到——实测记成 `tokens=0+0, cost=None`（消耗条上显示「未计价」）。但已生成那部分的 token 照样计费，所以**暂停轮的账面偏低**（实测约 0.005 元 vs 完整问答约 0.0095 元） |
| 暂停能力是单进程前提 | 取消标志用进程级 dict（ContextVar 跨线程/跨请求读不到）。多 worker 部署会失效——与「`task_utils` 从内存外移」是同一笔债，届时要一起搬 |
| 检查点降级期间无持久化 | Mongo 不可用时退回内存 saver，服务照常问答，但中断/续跑不可用、重启即丢；而且「**跑到一半** Mongo 挂掉」那一次问答会直接失败（降级只在建 saver 时判断）。详见「图检查点」一节 |
| 导入续跑会重跑「被杀的那个节点」 | 检查点只在**节点完成时**写，所以续跑从「最后完成的节点」的下一个开始 —— 被杀那一刻正在跑的那个会重来（杀在 MinerU 阶段就重烧一次解析配额）。见「导入侧：重启自动续跑」 |
| 挂起的确认轮会占到 TTL 到期 | 用户在卡片上不选、直接问别的，那一轮就永远挂在检查点里 —— 7 天 TTL 会自动清掉，但没有主动回收 |
| 确认轮的一次问答在账本上是两条汇总 | 两段共用同一个 `trace_id`（这是有意的：一次问答一个 trace），但每段各有一条汇总，按 trace 聚合成本时要自己相加 |
| 卡片里自己填的型号对不上库就按原样用 | **只问一轮，不再追问**（否则「对不上→再问」会绕不出来）；检索不到由生成节点的兜底答复收尾 |
| `("session_id", "ts")` 索引已无用武之地 | 历史排序改用 `_id` 之后（见「数据模型」一节），这个索引不再被查询用到。集合只有几百条、一次会话最多十几条消息，**留着不碍事**，所以没动 —— 真要收拾就建 `(session_id, _id)` 再删旧的 |
