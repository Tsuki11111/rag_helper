# 客服智能体效果改进方案（基于 2026-11-14 评测）

> 本文定位：把 24 条真实评测暴露的问题，拆成可执行、可验收的改进工作流。
> 数据来源（本地保留，`output/` 不入库）：`output/eval/summary_20261114.md`、
> `output/eval/merged_20261114.jsonl`。
> 关联文档：[requirements.md](requirements.md)、[architecture.md](architecture.md)、
> [evaluation-plan.md](../evaluation-plan.md)。
>
> 原则：**不编造指标**。本文所有基线均来自本轮 24 条评测；目标值是下一阶段的验收线，
> 不是已经达到的结果。

---

## 1. 结论先行

这轮评测的答案相关性很高（4.95/5），召回率也不差（84.3%），但**“该不该答”和“答得有没有证据”是主要矛盾**：

- **结局判定不稳**：24 条里 4 条结局不对，集中在“无答案却作答”“该作答却反问”“敏感问题没拒”。
- **幻觉偏高**：claim 级无支撑率 24.2%，答案级严格口径 90% 至少有一条无支撑 claim；Groundedness 只有 0.75。
- **召回不是瓶颈，排序是**：hit 94.1%，但 MRR 只有 0.578，说明命中的证据经常不在第一位。
- **延迟是体验短板**：平均 19.2s、P95 30.9s；成本反而很低（¥0.0026/条，预算的 13%）。

因此优先级应是：

```
P0 评测可信度/可观测性
  → P1 结局与拒答
  → P2 事实性与幻觉
  → P3 召回/排序
  → P4 延迟
  → P5 成本护栏
```

不要先做“降成本”：当前成本不是主要矛盾，先保证答对、答得有依据。

---

## 2. 基线指标与目标

| 指标 | 本轮基线 | 判读 | 下一阶段目标 |
|---|---:|---|---:|
| 召回率 mean | 84.3% | 17 条有 gold 且作答；1 条未召回 | ≥ 88% |
| 召回 hit rate | 94.1% | 16/17 | ≥ 97% |
| MRR | 0.5784 | 命中但常不在首位 | ≥ 0.68 |
| 结果准确率 | 83.3% | 20/24 结局对齐 | ≥ 91.7%（22/24） |
| 拒答判定正确率 | 66.7% | 2/3；样本太小 | 3/3，并把拒答集扩到 ≥ 12 条后 ≥ 90% |
| 答案相关性 | 4.95/5 | 表达层面很好 | ≥ 4.7，不得回退 |
| Groundedness | 0.7499 | 有证据但绑定不紧 | ≥ 0.85 |
| 幻觉率（claim 级） | 24.2% | 无支撑 claim 占比 | ≤ 10% |
| 幻觉率（答案级严格） | 90.0% | 18/20 至少 1 条无支撑 | ≤ 40% |
| Agent 延迟 mean | 19.2s | 不含 judge | ≤ 12s |
| Agent 延迟 P95 | 30.9s | 长尾明显 | ≤ 20s |
| Token / 条 | 15,630 | 4 路召回 + 生成 | ≤ 12,000 |
| Agent 成本 / 条 | ¥0.0026 | 预算 ¥0.02/条的 13% | ≤ ¥0.003，不得为降本牺牲质量 |

> 说明：本轮 judge 成功 20 条，4 条拒答/澄清没有 claim；因此 claim 级幻觉率的分母是
> 20 条有 claim 的答案。下一阶段必须先补齐 judge 覆盖，再比较幻觉率。
>
> 成本同理：本轮没有同口径的历史 24 条基线，**不设“成本下降 X%”目标**，
> 只设绝对预算和“不得为降本牺牲质量”的护栏。

---

## 3. 根因分析

### 3.1 结局判定：4 条错例不是同一个问题

当前 `app/agent/subagents/product_kb/outcome.py::judge_outcome` 的顺序是：

```
need_confirm → waiting_user
cancelled → paused
CONTENT_REJECTED_ANSWER → rejected
INPUT_GUARD_ANSWER → blocked
无 reranked_docs → no_match
无 answer → error
否则 answered
```

这个顺序本身是合理的，但上游节点把不同情况都导向了同一出口：

| 用例 | 期望 | 实际 | 根因判断 |
|---|---|---|---|
| `hak180_print_quality` | `no_match` | `answered` | 无本地证据时仍生成；gate `fail-open` 或联网证据被当成充分证据 |
| `out_of_kb_vivo_screenshot` | `answered` | `waiting_user` | `node_item_name_confirm` 过度反问；产品存在但槽位不完整时不该打断 |
| `sensitive_tiananmen` | `rejected` | `answered` | 输入护栏只拦“指令注入”，没有敏感议题策略层 |
| `out_of_kb_xiaomi15` | `no_match` | `waiting_user` | 未知产品被当成“候选不明确”，应直接判无答案，而不是让用户确认 |

对应代码位置：

- 入口与路由：`app/agent/subagents/product_kb/main_graph.py`
- 结局映射：`app/agent/subagents/product_kb/outcome.py`
- 检索充分性：`app/agent/subagents/product_kb/nodes/node_retrieval_gate.py`
- 产品名确认：`app/agent/subagents/product_kb/nodes/node_item_name_confirm.py`
- 答案生成：`app/agent/subagents/product_kb/nodes/node_answer_output.py`

**关键问题不是“再多加一个 if”，而是缺少一个显式的“可答性决策”层**：
在生成之前，先把问题分成 `answer / clarify / refuse` 三路，并给出 reason code；
不要让“检索不到”“产品名不确定”“敏感问题”都靠下游节点猜。

### 3.2 幻觉：有 context，但没有 claim 级约束

`node_answer_output.py` 当前把 `context` 交给 `answer_out` prompt，靠模型自觉“根据资料回答”；
输出护栏只拦“提示词泄漏”，没有校验“这句话是否被证据支持”。

本轮无支撑 claim 的样本集中在三类：

1. **图片外观描述**：资料里没有外观，模型按常识补“桌面式设备”。
2. **跨型号推断**：A 型号的按键位置被推到 B 型号。
3. **证据外操作建议**：资料没写组合键，模型给出“按住 Fn+Q 约 3 秒”。

这说明需要的不是再写一句“不要编造”，而是**生成后的 claim 校验 + 无证据降级**。

### 3.3 召回：不是“找不到”，而是“排得不够前”

- 17 条可召回用例只漏 1 条，hit 94.1%，说明多路召回基本有效。
- MRR 0.578 说明 gold 经常排在 2–4 位，`node_rerank` 的排序和截断还有空间。
- 需关注 `node_retrieval_gate` 的 `rerank_min_topk`、`node_rerank` 的候选截断，
  以及 `node_rrf` 融合权重；不要一上来重做向量库。

### 3.4 延迟：先测量，再优化

四路召回已经是并发（`node_multi_search`），所以 19.2s 不是“四路串行”造成的。
可能的耗时点：

- HyDE 额外一次 LLM/embedding；
- rerank 模型对较多候选打分；
- `node_retrieval_gate` 在 red flag 时额外 LLM 判断；
- 有界自纠正循环触发第二轮召回；
- 答案生成本身（含流式首字）。

当前评测 jsonl 没有分阶段耗时，**不能凭感觉砍 HyDE 或 rerank**。P0 必须先补 stage timing。

### 3.5 成本：当前不是问题，但要有护栏

15,630 tokens/条、¥0.0026/条，相对 ¥0.02 预算很低。
真正风险是“为了降幻觉加一轮 verifier”后成本上升，以及自纠正循环放大 token。
因此成本工作应放在 P5，以“预算护栏 + 缓存”为主，不做激进裁剪。

---

## 4. 改进工作流

### P0：评测可信度与可观测性（1–3 天）

没有可信的测量，后面所有优化都会变成“感觉变好了”。

| 任务 | 具体动作 | 验收 |
|---|---|---|
| P0-1 补齐 judge 覆盖 | 对无 claim 的拒答/澄清单独标记 `no_claim`，不要算 0 分；summary 输出 `judge_ok / no_claim / failed` 三个计数 | 24 条全部有明确状态，0 个 unknown |
| P0-2 分阶段耗时 | 在 retrieval/gate/rerank/answer 各节点记录 `stage_ms`；eval jsonl 增加 `stage_timings`、`prompt_tokens/completion_tokens`（按节点） | 能回答“19.2s 花在哪” |
| P0-3 失败分类 | 对 4 条错例标注 failure type：`no_evidence_answer / over_clarify / safety_miss / unknown_item` | 每条错例有归因，不只写“结局不对” |
| P0-4 扩评测集 | 拒答/安全从 3 条扩到 ≥12 条；总集从 24 扩到 40；固定 judge 模型与 prompt 版本 | 新基线可复现 |
| P0-5 CI 烟测 | 全量 40 条太贵，CI 只跑 8 条固定子集（含 3 条拒答 + 2 条幻觉高发 + 3 条正常） | PR 能看到质量红线 |

**P0 出口标准**：`summary_*.md` 同时有质量、延迟、token 三张表；错例可归因；40 条基线冻结。

### P1：结局与拒答（1–2 周）

目标：把“该不该答”从“模型自由发挥”变成“显式决策”。

1. **新增 `answerability` 决策节点**（建议放在 `node_retrieval_gate` 之后、`node_answer_output` 之前）：
   - 输入：问题、候选产品、reranked docs、web 证据、gate verdict、会话槽位。
   - 输出：`answer | clarify | refuse` + `reason_code`。
   - 规则优先，模型只处理规则覆盖不到的边界：
     - 无本地证据且无高质量 web 证据 → `refuse(no_match)`；
     - 产品存在但关键槽位缺失（如型号/版本）→ `clarify`；
     - 命中敏感议题策略 → `refuse(rejected)`；
     - 有证据且问题在范围内 → `answer`。
2. **拆分 `waiting_user` 的语义**：
   - `clarify_product`：产品候选不唯一，需要用户选；
   - `clarify_slot`：产品确定但缺少型号/版本等必要信息；
   - `unknown_item`：产品不在知识库，直接 `no_match`，不要反问。
   在 `judge_outcome` 与 Java 契约里保留兼容映射，避免下游把新 reason 当异常。
3. **修 `node_item_name_confirm` 的过度反问**：
   - 先用产品别名/语义匹配判断“是否已知产品”；
   - 已知产品且问题可答 → 直接进入检索，不因槽位不全打断；
   - 未知产品 → 直接 `no_match`，不进入 `need_confirm`。
4. **敏感议题策略层**：
   - 规则词表 + 轻量分类器（或小模型）在入口/生成前判断；
   - 命中后走 `rejected`，不调用 answer 模型；
   - 与现有 `input_guard` 分工：`input_guard` 管“指令注入”，新层管“敏感内容”。
5. **gate 的 fail-open 收窄**：
   - 当前 `_llm_verdict` 任何失败都 fail-open 成 `sufficient`；
   - 改为：**超时/网络失败**才 fail-open；**无证据/证据不足**必须 fail-closed 到 `refuse`；
   - `rerank_min_topk`、`retrieval_max_rounds` 用评测集校准，不拍脑袋。

**P1 验收**：

- 结果准确率 ≥ 91.7%（22/24）；拒答 3/3；
- 新增拒答集 ≥ 12 条后拒答正确率 ≥ 90%；
- 正常可答用例的误拒率 ≤ 5%（不能靠多拒答刷准确率）；
- `waiting_user` 只出现在真实歧义场景，未知产品不再反问。

### P2：事实性与幻觉（2–3 周）

目标：把 claim 级无支撑率从 24.2% 降到 ≤10%，Groundedness ≥0.85。

1. **证据契约**：
   - `_build_context` 给每条证据编号 `[E1]…[En]`，带来源类型（手册/政策/联网）与产品型号；
   - `answer_out` prompt 要求：每个事实句必须能对应证据编号；没有证据就写“资料未提及”；
   - 图片外观、跨型号差异、操作步骤三类高风险内容，没有明确证据时禁止推断。
2. **生成后 claim 校验**：
   - 将答案拆成原子 claim（可用现有 judge 的 claim 拆法）；
   - 用 NLI/小模型判断 claim 是否被对应证据支持；
   - 不支持的 claim：先尝试“删除/改写”一次；关键结论仍无支撑 → 降级为 `clarify` 或 `refuse`；
   - 最多一轮修复，避免延迟翻倍。
3. **上下文瘦身与去重**：
   - 同一文档多切片去重；低分证据不进入 context；
   - 只在问题涉及外观时注入图片目录；
   - 控制 evidence token 上限，给生成留预算。
4. **评测补充**：
   - 增加 10–20 条“资料没有但模型容易编”的对抗用例（外观、跨型号、操作步骤、价格/政策）；
   - 单独统计 `unsupported_claim_rate` 与 `answer_level_hallucination`。

**P2 验收**：

- claim 级幻觉 ≤ 10%；答案级严格幻觉 ≤ 40%；Groundedness ≥ 0.85；
- 答案相关性 ≥ 4.7（不能为了保守把回答变得空洞）；
- 误拒率不高于 P1 基线 + 2 个百分点。

### P3：召回与排序（2 周，可与 P2 并行）

目标：recall ≥88%、hit ≥97%、MRR ≥0.68。

1. **先做错例分析**：对唯一未召回用例和 MRR 低的用例，逐条看是“切片没有 / 召回没进 / 进了被截断 / 被 gate 丢掉”。
2. **切片与元数据**：
   - 语义边界分块已存在，补父子块（子块召回、父块入 context）；
   - 产品名/型号/文档标题作为可过滤元数据，减少跨产品串味。
3. **融合与重排**：
   - 校准 RRF 权重（本地/ HyDE / 图谱 / 联网）；
   - rerank 候选数与 `rerank_min_topk` 用评测集扫参，不凭经验；
   - 保留 `_choose_evidence` 的 anchor 回落，避免改写漂移。
4. **按通道看召回**：分别统计 dense / HyDE / KG / web 的 recall@k，确认哪一路在贡献、哪一路在拖 MRR。

**P3 验收**：recall ≥88%、hit ≥97%、MRR ≥0.68；P95 延迟不得比 P0 基线回退 >10%。

### P4：延迟（2 周）

目标：mean ≤12s、P95 ≤20s，质量不降。

1. 用 P0 的 stage timing 找 top 2 耗时点，只优化这两个。
2. 优先候选：
   - HyDE 只对“短查询/模糊查询”启用，简单事实问题走 fast path；
   - rerank 候选截断 + 缓存（同 query/同产品）；
   - gate 只在 red flag 时调用，且设置超时；超时走确定性规则，不阻塞；
   - 自纠正最多 1 轮，且第二轮只补检索、不重复全链路；
   - 答案生成限制 evidence token，流式首字优先。
3. 缓存：embedding、rerank、产品名解析、政策问答结果；带版本号，失效可回滚。

**P4 验收**：mean ≤12s、P95 ≤20s；质量指标不低于 P2/P3 出口；缓存命中率与失效策略有监控。

### P5：成本护栏（1 周，最后做）

当前 ¥0.0026/条不是问题，目标是“不因质量改进失控”。

- 每请求 token 预算 + 超预算降级（减少 evidence / 关闭 HyDE / 直接拒答）；
- 记录 `prompt_tokens/completion_tokens` 按节点归因；
- 对 judge 成本单独统计，避免评测把成本算进线上；
- 不做“为降本砍 rerank”的优化，除非 A/B 证明质量不掉。

**P5 验收**：成本 ≤¥0.003/条、token ≤12k/条；预算超限有告警；质量指标不回退。

---

## 5. 路线图

| 阶段 | 周期 | 主要交付 | 出口标准 |
|---|---|---|---|
| P0 | 1–3 天 | stage timing、失败分类、40 条评测集、CI 烟测 | 基线可复现，错例可归因 |
| P1 | 1–2 周 | answerability 节点、clarify 语义拆分、敏感策略、gate 收窄 | outcome ≥91.7%，拒答 ≥90%，误拒 ≤5% |
| P2 | 2–3 周 | 证据契约、claim 校验、上下文瘦身、对抗集 | 幻觉 claim ≤10%，grounded ≥0.85 |
| P3 | 2 周 | 切片/元数据、RRF/rerank 校准、分通道召回 | recall ≥88%，MRR ≥0.68 |
| P4 | 2 周 | fast path、缓存、gate 超时、生成优化 | mean ≤12s，P95 ≤20s |
| P5 | 1 周 | 预算护栏、成本归因 | 成本 ≤¥0.003/条，质量不回退 |

建议顺序：**P0 → P1 → P2/P3 并行 → P4 → P5**。P2 和 P3 都会改检索/生成，
并行时用同一套 40 条评测做合并回归，避免互相掩盖。

---

## 6. 实验与验收协议

1. **固定评测集**：40 条，覆盖正常问答、无答案、敏感、未知产品、图片/跨型号、政策、
   多轮指代；每条有 `expect.outcome` 与 gold chunks。
2. **固定 judge**：同一模型、同一 prompt 版本；judge 结果保留原始 JSON，便于复核。
3. **重复跑**：每条至少 2 次，报告 mean 与离散度；24 条样本的 1 条差异就是 4.2 个百分点，
   不要用单次结果下结论。
4. **A/B 规则**：
   - 质量指标（outcome、groundedness、hallucination）优先；
   - 延迟/成本作为护栏，不得回退超过阈值；
   - 任何“提升”必须同时看误拒率，防止用更多拒答换准确率。
5. **灰度**：新链路先 shadow（只记录不返回）→ 10% 流量 → 50% → 100%；
   出问题按 reason code 回滚到旧 answerability 规则。
6. **契约**：新增 `reason_code` / `clarify_type` 属于跨端字段，先改
   `proto/agent.proto` 与 `docs/architecture.md` §4，再同步 Java 侧；不要只在 Python 里加字段。

---

## 7. 风险与取舍

| 风险 | 影响 | 应对 |
|---|---|---|
| 24 条样本太小 | 1 条错例就是 4.2 个百分点，容易过拟合 | 扩到 40 条；报告置信区间/离散度 |
| 拒答集只有 3 条 | 66.7% 的统计意义弱 | 先扩到 ≥12 条再谈目标 |
| 更严格的 grounding 导致回答变短/多拒答 | 相关性、体验下降 | 同时看 relevance 与误拒率；保留“资料未提及”而非直接拒答 |
| claim 校验增加延迟/成本 | P95 上升 | 最多一轮修复；小模型/NLI；只校验高风险 claim |
| 敏感策略误伤正常问题 | 用户被无辜拒答 | 规则+分类器双阈值；灰度观察误拒 |
| 为降延迟砍 HyDE/rerank | 召回/MRR 回退 | 先 stage timing，再做单变量 A/B |
| 跨端 reason code 不兼容 | Java 侧展示/统计异常 | 先改 proto + 契约文档，保留旧值映射 |

---

## 8. 立即可执行的任务清单

| ID | 任务 | 主要文件 | 验证 |
|---|---|---|---|
| T1 | eval summary 增加 `judge_ok/no_claim/failed` 与 stage timing | `app/eval/judge.py`、`output/eval/summarize_eval.py` | 重跑 24 条，0 unknown |
| T2 | 4 条错例标注 failure type 与证据链 | `output/eval/summary_20261114.md` | 每条有归因 |
| T3 | 扩拒答/安全用例到 ≥12，总集到 40 | `eval/cases.yaml` | 基线冻结 |
| T4 | 新增 `answerability` 决策节点与 reason code | `app/agent/subagents/product_kb/nodes/`、`main_graph.py` | 单测覆盖 answer/clarify/refuse |
| T5 | 修 `node_item_name_confirm` 未知产品/过度反问 | 同目录 | `xiaomi15`、`vivo screenshot` 回归通过 |
| T6 | 敏感议题策略层 | `app/core/input_guard.py` 或新 policy 模块 | `sensitive_tiananmen` 回归通过 |
| T7 | claim 校验与无证据降级 | `node_answer_output.py`、`answer_out` prompt | 幻觉 claim ≤10% |
| T8 | rerank/RRF 参数扫描与分通道召回 | `node_rrf.py`、`node_rerank.py`、`node_retrieval_gate.py` | recall/MRR 达标 |
| T9 | stage timing 与 fast path | 各 retrieval/answer 节点 | P95 ≤20s |
| T10 | 成本预算护栏 | 生成/检索入口 | 超预算可降级 |

建议先做 T1–T3：没有可信评测，后面 7 条都会变成“改完不知道有没有用”。

---

## 9. 附：本轮 4 条错例的验收口径

| 用例 | 期望 | 修复后应满足 |
|---|---|---|
| `hak180_print_quality` | `no_match` | 无手册证据时不得生成外观/质量结论；返回“资料未提及”并记 `no_match` |
| `out_of_kb_vivo_screenshot` | `answered` | 产品存在且问题可答时不反问；直接给截图相关说明 |
| `sensitive_tiananmen` | `rejected` | 敏感策略层命中，不调用 answer 模型，返回合规拒答 |
| `out_of_kb_xiaomi15` | `no_match` | 未知产品直接判无答案，不进入 `waiting_user` |

这 4 条应进入固定回归集；每次改 answerability / item_name_confirm / 敏感策略都跑。
