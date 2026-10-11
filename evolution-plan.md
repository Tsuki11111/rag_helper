# 进化路线 · 从 RAG 服务到网购平台智能客服

> **本文档是进化路线的唯一权威。** README 只留指针与已建成成果的记录，HANDOFF 记执行状态。
> 前身已并入本文（2026-10-11）：`java-integration.md`（已删除，§0/§13/§14 等资产搬入本文）、
> README 的「企业化改造路线」与「评估路线」（已按新形态重置，见 §7/§8）。
> 决策的理由与审议过程见 [docs/decisions-evolution.md](docs/decisions-evolution.md)。

**一周窗口**：2026-10-11 → 11 中旬。投入：一个月全职强度（用户口径：AI 辅助下一个月已经很多）。

---

## 0. 一句话与验收标准

把「产品文档问答 RAG」进化为**网购平台智能客服**：deepagents 主 agent 编排，
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
                  ├─ subagent: order-query   ← 查订单（本月 mock，将来接 Java API）
                  └─ 将来的 subagent …
                调试入口：8002 HTTP/SSE（chat.html）· 8001 导入（管理端，HTTP 不动）
```

三条边界（继承，仅换协议）：

1. **浏览器只跟 Java 说话**——agent 服务只在内网、只允许那个单体访问
2. **agent 服务只认两个东西**：服务端密钥（平台/商户）+ `X-User-Id`（顾客）
3. **上传接口不接受 `X-User-Id`**——管理端行为，HTTP + 密钥那一档不变

**主 agent 是唯一的「嘴」**：用户可见的问题与澄清都由主 agent 出口。subagent 只干活、
只返回结果——这是 RAG 无状态化（§3）的根据，也是防「两张嘴打架」的红线。

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
| 流式 | 子 agent 进度/事件要 `stream_mode` 带 `subgraphs=True` 才在父流可见——chat.html 的统一执行流就接这里 |
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
| **M3** order-query | 第 2–3 周（可与 M4 并行） | 契约草案（订单号 → 状态/商品/时间…）；mock 工具（实现可替换，将来换 Java API）；复合场景 | 「我买的烫金机不会用，订单 X」→ 两个 subagent 都被调度 |
| **M4** gRPC | 第 3–4 周 | proto 定稿（§5）；`grpc.aio` 服务端复用业务函数；metadata 身份 → ContextVar；测试客户端 | 端到端：会话、流式事件序列、Stop 生效 |
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
- **T3 适配层**：`agent_process/main_agent.py` 的 `_rag_task`（消息进 / `AIMessage` 出；
  `need_confirm` → 候选文本；配图以 URL 附注；子调用包 `usage_context` 入账）
- **T4 prompt**：`prompts/main_agent.prompt` —— 产品问题第一步必须先检索、候选只准用
  检索返回的；**措辞要写到「必须遵守」级别**（qwen-plus 对软措辞不买账，实测两版才收敛）
- **T5 CLI 闭环**：两场景真跑（明确型号 / 无型号澄清）+ 日志核对记账
- **T6 用例**：本里程碑**未新增回归用例**（非修 bug）；「主 agent 派发」用例见 §8 待办

**验收**：CLI 两场景闭环；主 agent 调用入账；回归 36/36。

**状态：✅ 已完成（2026-10-11）** —— 两场景真跑通过；主 agent 调用确认入账（2 笔），
RAG 子调用 8 笔记在子 trace——**一次提问的完整成本需 join 两段**（归因补全见 §7）。
硬契约与 prompt 经验见 HANDOFF §3.33。**下一步 M3（order-query）**。

### M3 · order-query（模拟多 agent 调度）✅（2026-10-11）

**执行细案**（2026-10-11 定，用户明确收窄）：**只验多 agent 调度机制本身**，
不设计订单业务——这是「模拟」，不是「对接」：

- **T1 极简工具**：`agent_process/order_query.py` —— `query_order(order_no)` 返回
  **固定字符串**（带回订单号，便于验证参数透传）；不建 mock 数据、不设计字段契约
- **T2 声明式 subagent**：order-query 用 deepagents 的**字典式 SubAgent 声明**
  （tools + system_prompt，框架编译），不写图 —— 与 product-kb（CompiledSubAgent）
  凑齐两种 subagent 形态的示范
- **T3 prompt**：主 agent 说明里补 order-query 的调用时机与复合流程
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

**状态：✅ 已完成（2026-10-11）** —— 复合场景真跑：日志证实**两个 subagent 都被调度**
（rag_task 的任务描述里带着用户没提过的完整型号「Brother HAK 180 烫金机」，
只可能来自 order-query 的返回）；单场景只答订单。**额外收获**：把「复合流程」讲给主 agent 后，
它把「只问订单」也套了两段式（查完单顺手去检索产品用法，答非所问），
补一句「只问订单就只答订单」的边界才收敛 —— **教复合流程必须同时教它的边界**。
记录见 HANDOFF §3.34。**下一步 M4（gRPC）**。

---

## 5. gRPC 接口（草案，改写自原 java-integration §14）

**范围不变**：只有问答链路走 gRPC；导入/管理端继续 HTTP + JSON。
Python 侧「HTTP 给页面调试 + gRPC 给 Java」两套入口，业务逻辑共用。

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

**隔离验收**（对象已更新，排一个月后实施）：A/B 两个用户交叉试探——
① B 的 `ListSessions`/`GetHistory` 看不到 A 的会话；② 账本按 `(tenant, user)` 聚合各自对得上；
③ 管理端上传不受影响；④ 五条按项目规矩写成回归用例 + mutation check。

**必须 Java 侧拍板的事**（动工前确认）：① 顾客 ID 形态（数字/字符串、是否稳定）；
② 预期并发量级；③ gRPC deadline / keepalive 配置（≥ 180s + 余量）；
④ 是否要限流/配额（现在零限流，单调用方风险可控）。

---

## 7. 企业化改造（重置）

**已建成的 8 项**：成果全部保留，仅两处形态变化——
**访问鉴权**退化为纯服务端（Bearer + metadata；Cookie 只留自用调试）、
**checkpointer** 增加新用途（主 agent 的会话 thread，与导入侧续跑并存）。

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
3. 其余 36 条继续全量跑 —— ✅ 每批改动后全量跑（当前 36/36）

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

1. **Java 平台侧状态**（问了两轮未答）——不阻塞本月，决定真联调何时可排
2. 主 agent tool calling 实测（qwen-plus → 必要时 qwen-max）
3. gRPC proto 与查订单 mock 契约评审
