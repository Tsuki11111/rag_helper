# 进化路线 · 从 RAG 服务到一次买够平台客服智能体「买小购」

> **本文档是进化路线的唯一权威。** README 是项目门面（技术栈 / 架构 / 部署 / 进度）；
> 一切细节（含设计详档与操作手册）在 HANDOFF。
> 前身已并入本文（2026-10-11）：`java-integration.md`（已删除，§0/§13/§14 等资产搬入本文）、
> README 的「企业化改造路线」与「评估路线」（已按新形态重置，见 §7/§8；正文现收纳于 HANDOFF §8.2）。
> 决策的理由与审议过程见 [docs/decisions-evolution.md](docs/decisions-evolution.md)。

**一周窗口**：2026-10-11 → 11 中旬。投入：一个月全职强度（用户口径：AI 辅助下一个月已经很多）。

---

## 0. 一句话与验收标准

把「产品文档问答 RAG」进化为**一次买够平台的客服智能体「买小购」**：deepagents 主 agent 编排，
现有检索图降为 subagent，经 gRPC 接入 Java 购物平台。

**一个月验收（C 档）**：

1. **gRPC 服务化**——proto 定稿、服务端实现、测试客户端端到端（Java 侧 mock）
2. **RAG subagent 闭环**——主 agent 派发任务 → 检索图作答（带配图）→ 主 agent 转述
3. **只读「查订单」mock subagent**——两个 subagent 可由主 agent 组合调度
4. 自用界面（chat.html）呈现**统一的执行流**（主 agent 与 subagent 事件同一种渲染）
5. 全量回归通过；新链路按项目规矩补用例 + mutation check

**不在本月承诺内**：真联调 Java、数据隔离实现、写操作类 subagent。

---

## 1. 目标形态

```
浏览器 ──HTTPS──> Java 单体（登录、权限、页面、对浏览器的流式转发）
                    │  gRPC（内网，问答链路）
                    │  metadata：authorization: Bearer <服务端密钥>
                    │            x-user-id: <平台顾客 ID>
                    ▼
              Agent 服务（本仓库演化）· 单实例
                主 agent（deepagents）—— 唯一对用户说话的「嘴」
                  ├─ subagent: product-kb    ← 现有检索图（CompiledSubAgent）
                  ├─ subagent: product-guide ← 商品目录 / 推荐 / 语义检索
                  ├─ subagent: order-service   ← 查订单 / 下单草稿 / 退款草稿 / 催发货草稿
                  └─ 将来的 subagent …
                调试入口：8002 HTTP/SSE（chat.html）· 8001 导入（管理端，HTTP 不动）
```

三条边界（继承，仅换协议）：

1. **浏览器只跟 Java 说话**——agent 服务只在内网、只允许那个单体访问
2. **agent 服务只认两个东西**：服务端密钥（平台/商户）+ `X-User-Id`（顾客）
3. **上传接口不接受 `X-User-Id`**——管理端行为，HTTP + 密钥那一档不变

**主 agent 是唯一的「嘴」**：用户可见的问题与澄清都由主 agent 出口。subagent 只干活、
只返回结果——这是 RAG 无状态化（§3）的根据，也是防「两张嘴打架」的红线。

**统一询问入口（2026-10-12 立规矩）**：`agent/api/query_service.py` **不只是 product-kb 的 API**——
它是**整个询问服务的唯一入口**。用户问的任何事（产品问题 / 订单 / 将来的新能力），
都从这一个 API 进（HTTP/SSE；M4 起 gRPC 同源），内部一律由主 agent 编排：

- **新 subagent 不建自己的 API / 服务 / 端口**——只作为主 agent 的 subagent 接入，
  对外能力（会话、事件流、停止、用量）一律继承统一入口
- **事件流统一**：主 agent 与所有 subagent 的进度映射成同一种执行流（§5 的 `Trace`；
  M5 的灰字渲染对全部 agent 生效）
- **业务函数同源**：HTTP 的 `/query` 与 gRPC 的 `Converse` 复用**同一个主 agent 执行函数**
  （现状：`/query` 仍直连检索图——接主 agent 是 M4 服务化的第一步，见 HANDOFF §3.36）

**三条红线**（继承自原 java-integration §13，做任何业务 subagent 时都成立）：

1. **LLM / RAG 永远不直接写业务库**——只能「提议动作」，由 Java 侧校验并执行
   （「谁能退款、退哪一单」这类判定绝不交给模型）
2. **风控决策不用 LLM**——模型出信号、规则引擎做判定，否则出了资损没法解释、没法复现
3. **高危动作必须人工审批 + 全程审计**——Phase 4 那两项在客服场景从加分项变刚需

M3 的查订单**只读**，不触及①③，但边界先立在这、以后加写操作时照此执行。

---

## 2. 技术依据（deepagents 机制查证结果，2026-10-11）

| 项 | 查证结论 |
|---|---|
| 版本兼容 | 项目已在 `langchain>=1.3.15` / `langgraph>=1.2.11`；deepagents 0.7.23 要求 `langchain>=1.4.3,<2`——**小版本升级即可**，`uv add deepagents` 一并带入其依赖（会带上 `langchain-anthropic` / `langchain-google-genai`，不用可以不用） |
| subagent 机制 | 主 agent 通过 **`task` 工具**同步调用；**上下文隔离、只回最终结果**（官方定位 context quarantine）。现有图用 `CompiledSubAgent(name, description, runnable=<编译好的图>)` 直接接入 |
| 硬性要求 | **图的 state 必须有 `messages` 键**——现有自定义 state 需加适配 |
| 流式 | 子 agent 进度/事件要 `stream_mode` 带 `subgraphs=True` 才在父流可见——chat.html 的统一执行流就接这里（**M5 已按此实现并实测**，见 HANDOFF §3.42） |
| interrupt | 工具级 `interrupt_on` 与工具内 `interrupt()` 有官方支持；**图节点内 `interrupt()` 在 task 调用下无明文保证**——我们的无状态化（§3）正好绕开此不确定点 |
| 模型 | 主 agent 继续 DashScope（qwen，OpenAI 兼容端点）。**tool calling 质量直接决定派发效果，M2 先实测**，不稳则升 qwen-max（调用次数少，成本影响可忽略） |

**已知风险**：deepagents 0.7.x 生态仍年轻，坑未知——M1/M2 的实测步骤优先于一切纸面设计；
官方 troubleshooting 第一条就是「subagent 不被调用」，`description` 要写准。

---

## 3. RAG 改造：无状态化（本次唯一「推翻旧设计」处）

**改什么**（查询侧）：

| 项 | 现状 | 改成 |
|---|---|---|
| 产品确认 | 图内 `interrupt()`（`node_ask_user` + 检查点 + 卡片） | 认不出产品 → **返回候选**（`need_confirm`/`clarify` 字段已有），由主 agent 用对话问用户——**删逻辑不是加逻辑** |
| 会话历史 | RAG 读写 `chat_message`、多轮上下文自持 | **停用**（或仅存档）；对话上下文由主 agent 在 task 描述里带过来 |
| state | 自定义字段 | 加 `messages` 键 + 一层输入/输出适配（task 消息 → question；结果 → 答案 + 依据 + 配图） |
| 输出契约 | SSE 事件 | 结构化结果：答案文本 / 引用切片 / 配图 / 澄清候选（`response_format` 或约定字段） |

**为什么这不违背旧论证**：`java-integration.md` §13.3 曾论证「RAG 当工具、别当子 agent」，
理由是「两份会话状态打架」。而 deepagents 的 subagent 本质就是**工具形态的 agent 图**
（同步、无状态、单次返回），加上历史归主 agent——**仍然只有一份状态**。
旧论证的红线（「永远只有一份对话状态」）被保留，只是实现换了框架。

**保留不动**：四路检索 / RRF / 重排 / 生成 / 三层护栏 / 记账 / 运行记录 / 超时预算 /
故障分级重试——全套资产。

**退役**：查询侧图内 HITL（interrupt/resume/确认卡片）从主链路退场；
相关回归用例（产品确认、中断恢复）**改写**为「返回候选、不中断」的新断言。
导入侧完全不动（检查点、重启续跑照旧）。

---

## 4. 一个月里程碑

| 里程碑 | 参考周 | 内容 | 验收 |
|---|---|---|---|
| **M0** 文档合并 | 即刻 | 本文档 + decisions 落盘；删 `java-integration.md`；改各处引用；README/HANDOFF 同步 | 提交完成，无死链 |
| **M1** RAG 图改造 | 第 1 周 | 装 deepagents；state 加 `messages` + 适配层；去 interrupt（返回候选）；用例改写 + 新用例 | 命令行直接以「消息进 / 结果出」调用图跑通；回归全绿 |
| **M2** 主 agent | 第 1–2 周 | `create_deep_agent` + `CompiledSubAgent(product-kb)`；接 DashScope；tool calling 实测 | 本地 CLI 闭环：「烫金机怎么装膜」→ 派发 → 答案带配图；主 agent 调用是否入账一并确认 |
| **M3** order-service | 第 2–3 周（可与 M4 并行） | 契约草案（订单号 → 状态/商品/时间…）；mock 工具（实现可替换，将来换 Java API）；复合场景 | 「我买的烫金机不会用，订单 X」→ 两个 subagent 都被调度 |
| **M4** gRPC | 第 3–4 周 | proto 定稿（§5）；`grpc.aio` 服务端复用**主 agent 统一执行函数**（HTTP `/query` 同步迁到它，见 §1）；metadata 身份 → ContextVar；测试客户端 | 端到端：会话、流式事件序列、Stop 生效 |
| **M5** 执行流界面 | 第 4 周（≤2 天） | chat.html：泳道 → 灰色小字执行流；主 agent 与 subagent 事件统一渲染；对话气泡/配图保留 | 三场景演示时执行流可读 |

**贯穿**：每次改动守项目规矩（补离线用例 + mutation check）；触改动就同步 HANDOFF。
**月末演示**：三场景（产品问答 / 查订单 / 复合）+ gRPC 测试客户端 + 全量回归。

**以下每个里程碑独立一节：执行细案 → 验收标准 → 完成状态。**（M3 起：动手前先把细案写进来。）

### M0 · 文档合并 ✅（2026-10-11）

本文件与 [docs/decisions-evolution.md](docs/decisions-evolution.md) 落盘；删除 `java-integration.md`
（资产搬入 §5/§6/§9）；README / HANDOFF / evaluation-plan 各处引用改指；README 两节加「已重置」banner。
详见 HANDOFF §3.31。

### M1 · RAG 图改造 ✅（2026-10-11）

**执行细案**：

- **T1 装包**：`uv add deepagents`（langchain 升至 ≥1.4.3）——只装不接，升完先全量回归
- **T2 state 加 `messages`**：在 `QueryGraphState` 里声明（不声明会被静默丢弃，踩过）；
  输入适配「任务消息 → `original_query`」先跑通，输出端形状等 M2 实测 `CompiledSubAgent` 后定
- **T3 去 interrupt**：`route_after_item_name_confirm` 的 `need_confirm` 分支改到 **END**；
  摘掉 `node_ask_user` 的注册与接线；`node_item_name_confirm` 不动（照旧产 `need_confirm`/`clarify`）
- **T4 `node_ask_user` 缓冲式退役**：先从图里摘出 + 顶部标注「已随无状态化退役，勿修」，
  **M2 主 agent 闭环验证通过后再物理删除**；`_normalize_choice` 三级回退先检查新链路归属
  （confirm 节点承担 or 搬运），它守的「本地检索静默归零」必须有等价守卫；
  `_check_interrupt_resume`（回归清单「中断→恢复三条不变量」）随机制退役
- **T5 服务层**：`judge_outcome` 判据 `__interrupt__` → `need_confirm`（**保持 `waiting_user` 档**，
  否则这轮掉进 `no_match` 污染「知识盲区」报表）；`run_query_graph` 收尾的中断分支清理；
  `/resume`、`/pending` 端点保留（无挂起轮自然 409/空，清理留 M5）
- **T6 用例**：改 `main_graph.__main__` 场景 2 与 `_check_run_record` 的打桩；
  新增「认不出 → 图在 END 结束（无 `__interrupt__`）、带候选、`outcome=waiting_user`」
  + mutation check；同步 `regression.py` 清单

**验收**：回归全绿；命令行两场景（正常问答出答案 / 认不出直接结束带候选）；
`messages` 形式调用演示一次。**不做**：不接主 agent、不动前端与 SSE 样式、
不动 checkpointer 绑定、不动数据结构。

**状态：✅ 已完成（2026-10-11）** —— 回归 36/36（「中断→恢复」退役、
「认不出产品时图不再中断」新增、「手填型号对齐」挪至 confirm 节点）；
两处 mutation check 均抓住（route / judge_outcome 破坏都变红）；
端到端实测「烫金机盒怎么安装？」→ 图直接结束、3 个候选、无 `__interrupt__`、
`judge_outcome=waiting_user`。记录见 HANDOFF §3.32。

### M2 · 主 agent ✅（2026-10-11）

**执行细案**（按实际执行顺序回填——本里程碑动手时未先落细案，M3 起改为「先写细案再动手」）：

- **T1 摸契约**：直接读 `.venv` 里 deepagents 0.7.23 源码（`middleware/subagents.py`）：
  task 传子图 `{"messages": [HumanMessage(任务描述)] + 父 state 非排除键}`；子图须返回
  含 `messages` 的 dict，主 agent 取最后一条非空 `AIMessage` 的 text
- **T2 subagent 编译版**：`main_graph.get_subagent_app()` —— **不带 checkpointer**：
  task 会把父 config（含 thread_id）带进子图，共用 saver 会把两张图的检查点写串
- **T3 适配层**：`agent/main_agent.py` 的 `_rag_task`（消息进 / `AIMessage` 出；
  `need_confirm` → 候选文本；配图以 URL 附注；子调用包 `usage_context` 入账）
- **T4 prompt**：`prompts/main_agent.prompt` —— 产品问题第一步必须先检索、候选只准用
  检索返回的；**措辞要写到「必须遵守」级别**（qwen-plus 对软措辞不买账，实测两版才收敛）
- **T5 CLI 闭环**：两场景真跑（明确型号 / 无型号澄清）+ 日志核对记账
- **T6 用例**：本里程碑**未新增回归用例**（非修 bug）；「主 agent 派发」用例见 §8 待办

**验收**：CLI 两场景闭环；主 agent 调用入账；回归 36/36。

**状态：✅ 已完成（2026-10-11）** —— 两场景真跑通过；主 agent 调用确认入账（2 笔），
RAG 子调用 8 笔记在子 trace——**一次提问的完整成本需 join 两段**（归因补全见 §7）。
硬契约与 prompt 经验见 HANDOFF §3.33。**下一步 M3（order-service）**。

### M3 · order-service（模拟多 agent 调度）✅（2026-10-11）

**执行细案**（2026-10-11 定，用户明确收窄）：**只验多 agent 调度机制本身**，
不设计订单业务——这是「模拟」，不是「对接」：

- **T1 极简工具**：`sub_agents/order_service_agent/order_service.py` —— `query_order(order_no)` 返回
  **固定字符串**（带回订单号，便于验证参数透传）；不建 mock 数据、不设计字段契约
- **T2 声明式 subagent**：order-service 用 deepagents 的**字典式 SubAgent 声明**
  （tools + system_prompt，框架编译），不写图 —— 与 product-kb（CompiledSubAgent）
  凑齐两种 subagent 形态的示范
- **T3 prompt**：主 agent 说明里补 order-service 的调用时机与复合流程
  （先查单拿型号 → 再喂给检索）
- **T4 用例**：**不做**（固定字符串没有可守的行为；项目规矩针对的是「静默失效」，
  此处无失效面）——回归维持 36 条
- **T5 验收**：复合场景真跑「我买的烫金机不会用，订单 12345」→ 日志确认
  **两个 subagent 都被调度**、回答结合订单信息与使用指导；单场景顺带跑一次

**不做**：订单契约 / mock 数据 / 字段设计 / 归属校验 / 物流实时刷新 / 真 Java API ——
**全部推给「真正接 Java 时」**：届时按真接口现场设计（比现在拍脑袋造假的强）。

**自测发现的修复**（2026-10-11 用户实测暴露，动手前定案）：

现象：用户确认「GANSS GS3104T-PRO 机械键盘」后，主 agent 一字不差回灌标准名，
RAG **仍报候选**——`GS3104T-PRO` 与标准名的向量相似度反复卡在 0.6~0.85 区间，
形成「确认 → 又候选 → 再问」的循环（旧链路里「用户确认后无条件采用」随
`node_ask_user` 一起退役了，新链路没补等价物）。

修法（**字面匹配优先、向量兜底**；kb_item_names 全库只有 9 条，本地全量匹配零成本）：

- **T1 字面三级规则**（成熟三板斧的确定性版本）：Unicode NFKC 规范化 ＋ casefold
  ＋ 去分隔符 → ① 精确相等 ② 子串包含 ③ **token 集合包含（顺序无关）**；
  约束：长度下限 ＋ **唯一命中**（多命中退候选）
- **T2 确认闭环**：字面直通 → 向量 ≥0.85 → **都不中才问用户**；用户确认后主 agent
  回灌候选标准名原文 → 字面必中 → 不再循环
- **T3 prompt**：主 agent「重发时用候选里的标准名原文」＋「检索找不到时不许凭通用知识编造」
  （实测它曾自己编键盘快捷键）
- **T4 用例**：直通命中（`GS3104T Pro` / 回灌原文 / `hak180烫金机`）、顺序重排
  （`RS-12 万用表`）、短词不直通、多命中退候选 + mutation check

**状态：✅ 已完成（2026-10-11）** —— 字面匹配**一次直通**（真跑用户 case：
`GS3104T Pro` → 日志 `字面匹配：命中=['GANSS GS3104T-PRO 机械键盘']，转向量=[]`，
零追问直接检索出真实答案）；用例扩展 + mutation check 抓住；回归 36/36。
记录见 HANDOFF §3.35。

### M4 · 服务化 + gRPC

**执行细案**（2026-10-12 定）：把统一询问入口落地——HTTP `/query` 迁到主 agent，
gRPC `Converse` 复用同一执行函数；**平台 infra 同步上移**（挂点对照见 §7）。

- **T1 统一执行函数 `run_main_agent`**（放 `main_agent.py`）：一轮询问的骨架都在这——
  **输入护栏先过**（不合规直接拒、不烧模型调用）；**整轮 `usage_context`**（trace = run_id、
  整轮 wall-clock 预算）；会话状态**复用 `chat_message`**（读 `get_recent_messages` 组装
  messages、写 `save_chat_message`；**不接 checkpointer**，状态源就是会话历史）；
  **运行记录 `query_runs` 在这层写**（一轮一条）；产出结构化事件流（`step` / `final` / `error`）；
  主 agent 的模型调用纳入既有重试与异常策略
- **T2 RAG 停用 chat_message 读写**（无状态化收尾）：`step_1_get_history` 不读、
  `step_2`/`step_7` 不写、`node_answer_output` 不存档不读历史；相关回归用例随写者迁移
  （历史顺序 / 会话列表 / 配图往返 / 建历史——载体不变，断言改到主 agent 侧）
- **T3 `/query` 迁移**：query_service 改调 `run_main_agent`（替换 `run_query_graph`）；
  `step` 事件推为 `trace` 类型 SSE（chat.html 暂忽略，M5 渲染）；`/sessions`、`/history` 保持；
  `/stop` 作用于整轮；运行记录不再在服务层写
- **T4 gRPC**：proto 定稿（§5）；gRPC 服务端（**同步 grpc + 线程池** —— 业务函数
  `run_main_agent` 是同步的，aio 反要线程桥接）= `run_main_agent` + 事件映射；
  测试客户端端到端 —— **Java 未开工时「端到端」= Python 测试客户端扮演 Java**：
  走真实 HTTP/2 + 真实 protobuf 序列化 + 真实 metadata 身份（不是 mock）；
  覆盖 proto 契约、流行为（事件序列 / 会话续接 / Stop）、身份读取、**与 HTTP 路径
  的行为等价**；跨语言代码生成与 Java 侧网络配置留给联调（本就不在本月承诺内）。
  该客户端同时是**给 Java 侧的参考实现**（proto + 可跑样例 = 最小接口文档）
- **T5 身份**：metadata → `usage_context` 的 ContextVar（最小版，tenant/user 留位）
- **T6 验证**：回归全绿（用例迁移后）+ HTTP 与 gRPC 双端到端（会话续接、停止）
  + mutation check

**验收**：同一会话经 HTTP 与 gRPC 各跑多轮，历史一致；chat.html 能用主 agent 连续对话；
运行记录 / 记账 / 护栏的挂点按 §7 对照表就位。

**状态：✅ 已完成（2026-10-12）** —— T1–T6 全部落地：`run_main_agent` 统一执行函数
（HANDOFF §3.37）→ RAG 停用会话历史读写（§3.38）→ `/query` 迁移 + `/resume` `/pending`
退役 + `trace` 事件（§3.39）→ proto 定稿 + gRPC 服务端（8003，与 HTTP 复用同一执行
函数）+ 测试客户端 / Java 侧参考实现（§3.40）。验证：回归 34/34；
**HTTP 与 gRPC 双端到端真跑**（gRPC 侧含会话续接、Stop、鉴权拒绝）。

**状态：✅ 已完成（2026-10-14）** —— chat.html 泳道换灰字执行流：
`_consume_stream` 开 **`subgraphs=True`**（§2 的机制实测落实），主 agent 与 subagent
事件统一渲染（子图节点 → `node` 事件、子 agent 工具调用 → `tool` 事件）；
顺带清掉 §3.38 的**配图通道**待办（结构化 images 贯通 final / 会话存档 / gRPC
`FinalEvent`）与 M4-T3 遗留的「联网」开关静默断线。验证：回归 **37/37**
（新增 3 条 + 3 条 mutation 全抓住）；服务端四路径 E2E 真跑（见 HANDOFF §3.42）；
其间还修了一处**派发静默失败**（模型凭常识预判「不是平台商品」而跳过检索，
提示词两轮加固，见 HANDOFF §3.43）。**M0–M5 全部完成，一个月验收（C 档）五项就绪。**

**状态：✅ 已完成（2026-10-11）** —— 复合场景真跑：日志证实**两个 subagent 都被调度**
（rag_task 的任务描述里带着用户没提过的完整型号「Brother HAK 180 烫金机」，
只可能来自 order-service 的返回）；单场景只答订单。**额外收获**：把「复合流程」讲给主 agent 后，
它把「只问订单」也套了两段式（查完单顺手去检索产品用法，答非所问），
补一句「只问订单就只答订单」的边界才收敛 —— **教复合流程必须同时教它的边界**。
记录见 HANDOFF §3.34。**下一步 M4（gRPC）**。

### M6 · 检索循环（有界自纠正，可选增强）✅（2026-10-20）

**结论**：agent loop 加在**检索子图内部**（`node_rerank` 之后），不在主 agent 层重复建设 ——
主 agent 本身已是 deepagents 的 model↔tools 循环；检索子图原本是固定 DAG，一次检索定生死。

```
node_rerank → node_retrieval_gate
  ├─ 充分 → node_answer_output
  ├─ 不足且轮次/预算允许 → node_query_rewrite → node_multi_search（下一轮）
  └─ 不足但达上限/预算不够 → node_answer_output（用第一轮证据）
```

**四条防漂移红线**：① 意图锚与检索探针分离（作答/重排用 `rewritten_query`，
循环只改 `retrieval_query`）；② 判定永远对 `original_query`；③ 改写必须保留标准产品名、
不得重复历史检索词；④ 循环耗尽回落第一轮证据。

**默认关闭**（`RETRIEVAL_MAX_ROUNDS=1`，不配 `.env` 行为与从前一致）；实测后建议设 2。
判定器只在红灯轮次调用（无证据 / 纯联网 / 重排降级 / 本地过少），正常问答零额外调用；
失败 fail-open 成「充分」；剩余预算不足 45 秒不重试。
**它不是评估体系的 LLM-as-Judge（P3）**，判定结果不得拿去算检索质量指标。

**验证**：离线用例 5 条 + mutation 6/6 抓住；图级桩测试三场景（恢复 / 耗尽回落 / 守卫拒绝）；
全量回归依赖未起时 28 通过 / 20 跳过 / 0 失败，**8 个容器起齐后 48/48 全通过**；
真实链路已跑（正常问题不重试；知识库未覆盖问题走完 retry → 改写 → 第二轮 → 达上限回落，
答案无漂移）。详见 **HANDOFF §3.52**。
**下一步**：24 条 gold 用例跑「关闭 vs 开启」对比，再决定默认值是否改为 2。

---

## 5. gRPC 接口（草案，改写自原 java-integration §14）

**范围不变**：只有问答链路走 gRPC；导入/管理端继续 HTTP + JSON。
Python 侧「HTTP 给页面调试 + gRPC 给 Java」两套入口，**复用同一个主 agent 执行函数**
（统一询问入口，见 §1）。

```proto
syntax = "proto3";
package agent.v1;
option java_multiple_files = true;

service AgentService {
  // 发一条用户消息，事件流回（一次调用、事件流回——流的归属天然就是这次调用，
  // 不存在「订阅到别人的会话」）
  rpc Converse (ConverseRequest) returns (stream Event);
  rpc Stop (StopRequest) returns (StopReply);            // 停止本轮生成
  rpc ListSessions (ListSessionsRequest) returns (ListSessionsReply);
  rpc GetHistory (HistoryRequest) returns (HistoryReply);
  // 预留（v1 不实现）：高危动作审批的 Decision RPC——将来出现写操作时再加
}

message ConverseRequest {
  string session_id = 1;         // 空则服务端生成
  string message = 2;            // 用户消息（自然语言，可能包含订单号等）
}
message StopRequest { string session_id = 1; string run_id = 2; }
message StopReply { bool stopped = 1; }

// 一条事件；UI 的「灰色小字执行流」直接消费 Trace
message Event {
  oneof kind {
    SessionStart start = 1;      // session_id + run_id（客户端存下来）
    Trace trace = 2;             // ★ 统一执行流：主 agent 调度 / subagent 事件（含原泳道的节点进度）
    Delta delta = 3;             // 流式增量（最终给用户看的文本）
    Usage usage = 4;             // 累计用量
    FinalEvent final = 5;        // 本轮结束：完整回复 + 配图
    ErrorEvent error = 6;        // 出错（不叫 Error，避免与 java.lang.Error 撞名）
  }
}
```

**与旧草案的两处实质变化**：

- **`Confirm` / `Resume` 消失**——产品确认改由主 agent 对话承接（§3）；
  每轮都是新 `Converse`，主 agent 的 thread 自己延续，无 resume 语义。
  HITL 审批语义留给将来的写操作（上面预留的 Decision RPC）
- **新增 `Trace` 统一事件**——替代「泳道」的 `Progress`：主 agent 的调度步骤、
  subagent 的图内节点事件，都映射成同一种带文本的行（`kind/title/detail`），
  chat.html 与 Java 前端用同一个渲染规则

**换协议不换规矩**（继承 §14.3/§14.4）：deadline ≥ 一轮墙钟预算（180s）+ 余量；
keepalive 替代应用层心跳（不用自己写）；`run_id` 仍由本服务自生成，
平台存「平台请求 ↔ run_id」映射，**不透传**当 thread_id（撞 id = 共用检查点）。

---

## 6. 平台对接前提（修订继承）

**已拍板**（拓扑修订处已标注）：

| 项 | 定论 |
|---|---|
| 拓扑 | Java **单体** + Agent 服务（原「RAG 服务」演化，仍是单实例）——不经网关、不上微服务 |
| 身份模型 | **内网信任 + `X-User-Id`**（走 gRPC metadata）：agent 服务只在内网、只有一个调用方；Java 注入时**先剥离客户端同名 metadata 再写入** |
| 身份两维 | `tenant_id`（平台/商户，密钥换来）+ `user_id`（顾客，`X-User-Id`）——`user_id` 必须走 `usage_context` 的 ContextVar（LangGraph 子任务 `copy_context()`，这是并发下唯一的串号来源） |
| 知识库 | **全平台共享一份**；上传是服务端行为 |
| 要分的东西 | **只有「人」的痕迹**：主 agent 会话（thread）/ 账本 / 运行记录 |
| 不做 | 不动 Milvus / Neo4j / MinIO；不给上传加 `user_id` |
| 缺头行为 | 本机/自测放行（固定值如 `local`）；生产直接 400 |

**工具回调（M7 起新增的对接形态，2026-10-21）**：

- **方向**：agent → Java，HTTP + JSON，内网。浏览器仍只跟 Java 说话。
- **接口**：`POST /internal/agent/orders/query`、`POST /internal/agent/catalog/products`、
  `GET /internal/agent/catalog/all|categories`、`POST /internal/agent/actions/order|refund`；
  请求体只有业务参数，**没有 userId**。
- **身份**：Java 每次 Converse 签发短期票据（Redis，10 分钟），经 gRPC metadata
  `x-agent-tool-ticket` 下发；agent 回调时带回 `X-Agent-Ticket`。Java 只认票据里的
  userId，模型不参与身份。
- **契约**：`proto/agent.proto` 不变；这是平台侧的 HTTP 对接，不新增 gRPC RPC。
- **数据最小化**：订单视图不含收货人 / 电话 / 地址。
- **写操作（M9/M10 已落地）**：agent 只 `prepare`，Java 落 `agent_actions` 的
  PENDING_CONFIRM 快照；用户在平台聊天卡片确认后，Java 按服务端快照调用现有业务
  Service 执行。确认接口只收 actionNo，参数全部来自服务端快照；agent 不能确认，
  模型也不能指定用户 / 金额。催办类低危动作（M8，暂未做）可直接执行。
- **商品卡（M12 已落地）**：`FinalEvent` / `HistoryMessage` 增加
  `repeated ProductCard products`；Java 映射到 SSE final / history；
  商品目录走 Java 内部工具接口，模型只选筛选参数，**不写 SQL**。
- **物流（M7.5 已落地）**：order-service 返回物流摘要（status / eta / carrier /
  courier / progress），地址不进 agent；物流模拟在 Java 侧，**ETA = 最终送达时间**。

**隔离验收**（对象已更新）：A/B 两个用户交叉试探——
① B 的 `ListSessions`/`GetHistory` 看不到 A 的会话；② 账本按 `(tenant, user)` 聚合各自对得上；
③ 管理端上传不受影响；④ 五条按项目规矩写成回归用例 + mutation check。

**进度（2026-10-20）**：① 已完成 —— `chat_message` 带 `user_id`、新增 `chat_session`
归属表，gRPC ListSessions/GetHistory 按 `x-user-id` 过滤，Converse/Stop/GetHistory
校验 owner；Java 侧 `agent_sessions` 再做一次归属过滤。④ 回归用例已加
（`mongo_history_clients` 自测 + Java 会话列表测试），mutation check 尚未做。
② 账本按用户隔离、完整 A/B 验收仍待补。

**必须 Java 侧拍板的事**（已拍板 2026-10-20）：① 顾客 ID = MySQL BIGINT，注入时转字符串；
② 单实例、低并发；③ gRPC deadline 240s、keepalive 300s；④ Java 侧按用户限流
（客服对话 10 次/分钟），agent 侧仍不单独限流。
**已拍板 2026-10-21**：⑤ 工具回调走内网 HTTP；票据 Redis TTL 10 分钟，
`/internal/agent/**` 不对外暴露。

---

## 7. 企业化改造（重置）

**平台上移原则（2026-10-12 立）**：此前为 product_helper（检索图）构建的全部 agent infra——
记账、日志归因、运行记录、护栏、预算、重试、鉴权、暂停——**一律上移为主智能体的平台层**，
挂点在统一执行函数（`run_main_agent`）与各 subagent 接驳层，不再属于某一张图：

| infra | 原挂点（product_helper 配套） | 主智能体时代 |
|---|---|---|
| 记账 / 用量 | RAG 节点 + 调用点 | 主 agent 调用 + 各 subagent 全覆盖；**整轮成本 join 两段 trace**（补全见下） |
| 日志 / 归因 | 图节点 + 入口 | 统一执行函数设 trace，覆盖主 agent 与全部 subagent |
| 运行记录 `query_runs` | `run_query_graph`（检索图一轮） | **`run_main_agent`（一整轮询问）** |
| 输入护栏 | RAG confirm 节点 | **统一入口先过**（不合规不烧主 agent 调用）+ 子图纵深保留 |
| 超时 / 预算 | 查询图 `tracked_node` | **整轮包在 `run_main_agent`** |
| 故障重试 | 客户端 + RAG 调用点 | 主 agent 的模型调用同样走 `invoke_with_retry` |
| 暂停 stop | RAG 图 `active/stop` | 同一机制作用于整轮 |
| 鉴权 | HTTP Cookie / Bearer | 统一入口（HTTP 保留 + gRPC metadata，§6） |
| 评估 / 回归 | 检索质量 | 主 agent 编排质量 + subagent 质量（§8） |
| checkpointer | RAG 图（**已随无状态化退役**） | 导入侧续跑保留；主 agent 暂不接（状态源 = 会话历史） |

**已建成的 8 项**：成果全部保留，形态变化在**访问鉴权**——退化为纯服务端
（Bearer + metadata；Cookie 只留自用调试）。

**剩余项重置后的排期**：

| 原项 | 新形态下的修订 | 排期 |
|---|---|---|
| Phase 3 数据隔离 | 对象改为「主 agent 会话 + 账本 + 运行记录」（RAG 的 `chat_message` 停用后不再需要） | **一个月后第一项**（接口先留位） |
| Phase 3 成本归因 | 扩展为 agent 调度层：主 agent LLM 调用入账（M2 顺带验证 LangChain callback 是否覆盖 deepagents） | M2 验证 + 一个月后补全 |
| Phase 3 并发槽位 / 限流 | 单实例 + 内网 + 单调用方，风险可控 | 挂起（多调用方时重估） |
| Phase 4 Guardrails | 输入/输出护栏继续；**工具护栏 = 业务 subagent 的动作确认**（与客服红线合流） | 出现写操作时 |
| Phase 4 审计/审批/版本管理 | 查订单只读，暂无审批对象 | 挂起（写操作出现时为刚需） |

---

## 8. 评估（重置）

**对象扩展**：从「检索质量」到「**主 agent 编排质量 + subagent 质量**」——
派发正确率（该不该调、调谁）、澄清时机、子图质量（原指标全部保留）。

**本月只做三件事**（其余冻结）：

1. 产品确认相关用例**改写**（图不再 interrupt，断言「返回候选」）—— ✅ M1 已完成
2. 新链路补 2–3 条用例：主 agent 派发、无状态 RAG 的候选返回、gRPC 端到端之一
   —— 部分：候选返回 ✅（M1 路由级用例）；主 agent 派发 ✅ 真跑验证（M3，无自动化用例）；gRPC 端到端待补
3. 其余用例继续全量跑 —— ✅ 每批改动后全量跑（2026-10-20 起 **50 条**；
   本机依赖未起时按依赖标 SKIP，失败才返回 1）

**一个月后**：P2 gold 扩样（取决于语料决策）、P3 LLM-as-Judge（等「换提示词/换模型」的真决策出现）、
agent 层指标（派发/澄清类）随评估对象一起设计。原「评估路线」文档的 6 阶段拆分仍有效，对象换新。

---

## 9. 明确不做 / 按需区

**本月明确不做**：数据隔离实现、多租户、审计/审批、评估 P2 扩样与 P3、
SSE 跨进程改造、限流、微服务化。

**按需区（搬运自原 java-integration，将来需要时不再重新论证）**：

- **若 Java 侧改微服务**：`X-User-Id` 改网关注入（剥离同名头，规则进网关路由）；
  逐跳检查流式/缓冲/超时；服务间凭据升级（静态密钥 → mTLS/OAuth2）；
  限流落网关、token 预算留本服务。**gRPC 使「订阅归属」问题天然不存在**，
  但多实例下主 agent 的流与 thread 仍需路由一致性（sticky）
- **流式韧性**（原 §12）：应用层心跳 → gRPC keepalive 已替代（Java→agent 段）；
  Java→浏览器的 SSE 心跳/超时是 Java 侧事务；断流重连与轮询兜底的语义在 gRPC 下待定义，
  有真实反馈后再做
- **SSE 那笔债**：自用路径（chat.html 直连）仍用 SSE + 进程内队列——**单实例即可**；
  生产路径走 gRPC，不受此影响

---

## 10. 待定问题

1. ~~**Java 平台侧状态**（问了两轮未答）~~ —— **已确认（2026-10-13）**：
   ItsMyGo 在 `C:\Users\28470\IdeaProjects\ItsMyGo\`，已开工、自带完整文档体系
   （其 HANDOFF：M1 账号体系完成于 2026-10-12，下一步 M2 商品与分类），
   **其文档以本仓库 §5/§6 + `proto/agent.proto` 为契约权威**。
   **真联调何时可排仍取决于对方进度**，不阻塞本月
2. ~~主 agent tool calling 实测~~ —— **已定（2026-10-14）**：qwen-plus 同一提示词下
   实测 4/6 会跳过检索（提示词已收敛，属模型随机性；qwen-max 被用户否决），
   **主 agent 改用 DeepSeek 官方 API 直连（`deepseek-flash`，实测 7/7 全派发）**。
   配置与验证见 HANDOFF §3.43–§3.45（§3.45 顺手把主 agent 的模型调用接上了
   项目统一的暂时性故障重试——M4-T1 列过、当时未落地的目标）
3. gRPC proto 与查订单 mock 契约评审
