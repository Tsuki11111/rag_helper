# 会话内表单协议 · 评审稿（FormRequest v0，待评审）

> 状态：**Java 侧已评审通过（2026-11-07）**，7 个决策点均已答复（见 §9）；按 v0 可改
> `proto/agent.proto`，实现待排期。
> 日期：2026-11-07 ｜ 需求依据：`docs/requirements.md` §2.10 ｜ 契约说明：`docs/architecture.md` §4

## 1. 背景与目标

现在需要用户补充结构化信息时，agent 只能用自然语言追问（例如「把收货地址 id 发我」），
用户要在聊天里手打 id、规格、原因，体验差且模型容易猜错参数。目标是：

1. agent 能**请求平台渲染一张表单**（改地址 / 选规格 / 选订单 / 选退款原因）；
2. 用户填写后，字段值**结构化回传**给 agent，agent 按 `form_type` 直接调用对应工具，
   不再从自然语言里猜参数；
3. 表单请求随 `FinalEvent` 下发、随历史消息可回放，旧客户端/旧 AI 侧都能安全忽略。

**非目标**：本期不做表单的富交互（联动、校验器插件）、不做任意 HTML 注入。

## 2. 建议契约（proto 草案，未生效）

```proto
message FormField {
  string name = 1;                 // 回传字段名，agent 侧按它取值
  string label = 2;                // 展示名
  string type = 3;                 // text | number | select | date
  bool   required = 4;
  string placeholder = 5;
  string default_value = 6;
  repeated string options = 7;     // select 用，格式 "value|label"
}

message FormRequest {
  string form_id = 1;              // 幂等 id（重发不重复消费）
  string form_type = 2;            // address_edit | sku_select | order_pick | refund_reason
  string title = 3;
  repeated FormField fields = 4;
  string submit_label = 5;
  int32  expires_in_seconds = 6;   // 建议 600；过期后前端置灰
}

// FinalEvent / HistoryMessage 增加：
// repeated FormRequest forms = N;
// ConverseRequest 增加：
//   FormResult form_result = N;    // { form_id, map<string,string> values }
```

**为什么不把表单塞进 answer 文本**：前端无法可靠解析，历史回放也会变形；结构化通道
（与配图 / 商品卡同一套路）才能让前端稳定渲染。

## 3. 回传路径（两个方案，推荐 A）

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | 前端提交表单 → 作为**下一轮 `ConverseRequest` 的 `form_result`** 发给 Java → Java 透传 gRPC → 主 agent 按 `form_type` 调工具 | 与现有「一轮一条 Converse」模型一致；票据、限流、审计、历史都复用；agent 不暴露给前端 |
| B | 前端提交后**直接调业务接口**（改地址/加购/退款），只把结果文本告诉 agent | 绕过 agent 与票据体系，业务口径两套；不采用 |

**主 agent 处理 `form_result` 的规则**：`form_id` 必须是本会话最近一次下发的表单
（防重放）；按 `form_type` 调对应工具（例如 `address_edit` → 调地址更新工具），
**不再让模型拼参数**；工具失败按行动能力三分处理（见 implementation §5.8）。

## 4. 兼容与灰度

- 旧 Java 不认识 `forms` 字段 → protobuf 忽略，前端仍渲染旧的文本追问；
- 旧 rag_helper 不认识 `form_result` → 降级为普通文本消息（忽略），不影响对话；
- 灰度开关：Java 配置 `itsmygo.agent.forms.enabled=false` 默认关闭，前端按事件类型渲染。

## 5. 安全

- `form_id` 服务端一次性消费（Redis SETNX，TTL=有效时长），防重放/重复提交；
- 字段白名单（服务端按 `form_type` 校验允许的 name），拒绝任意字段注入；
- 表单值属于用户输入：**不进日志明文**（含手机号/地址），仅记 `form_id + 字段名`；
- 仍走原有票据鉴权与限流；前端不接触 agent 内部参数。

## 6. Java 侧改动清单（评审后评估工作量）

**契约（双方）**：`proto/agent.proto`（本仓库是源头）加 §2 三个 message + 两个字段；
`docs/architecture.md` §4 契约概览同步。

**Java 后端**：
1. gRPC 侧把 `FormRequest` 透传到 `FinalEvent`，随 SSE 下发；
2. `ConverseRequest` 接收 `form_result` 并转发 gRPC；
3. `form_id` 一次性消费（Redis）+ 字段白名单校验；
4. 历史消息保存/回放表单：**修正**——历史实际由 rag_helper 的 Mongo 存，Java 侧只需扩展
   `AgentHistoryMessage` DTO 映射（Java 没有 `agent_history` 表，不需要加表/加列）。

**Java 前端**：
5. `FinalEvent` 出现 `forms` 时渲染卡片表单（select/number/text）；
6. 提交后把 `form_result` 放进下一轮 Converse；
7. 过期/已提交状态置灰。

**测试**：Java 单测（透传、白名单、重放拒绝）+ 前端 E2E（渲染、提交、回填后 agent 正确调工具）。

## 7. 待评审决策点（需要 Java 侧明确回答）

> ✅ 2026-11-07 已由 Java 侧（ItsMyGo）逐条答复，结论见 §9。

1. **回传载体**：同意方案 A（`ConverseRequest.form_result`）还是希望复用消息 part 结构？
2. **字段值类型**：统一 `map<string,string>`（前端做类型转换）还是 protobuf `Struct` / 分类型字段？
3. **`forms` 放哪**：`FinalEvent` 顶层（推荐）还是嵌进 `HistoryMessage` 的 parts？
4. **历史回放**：表单请求是否必须存档并随历史返回（推荐是，否则用户刷新后卡片丢失）？
5. **过期策略**：TTL 600s 够不够？过期后是前端置灰还是后端拒收？
6. **多表单**：一轮是否允许多张（如同时选规格+改地址）？建议**最多 1 张**，避免前端爆炸。
7. **哪几个 form_type 先做**：建议第一批只做 `refund_reason`（最小闭环，不依赖新业务接口），
   第二批 `sku_select` / `address_edit`（改地址需要新的更新接口）。
   > Java 侧补充：`address_edit` **不需要新接口**，ItsMyGo 已有 `PUT /api/addresses/{id}`；
   > 建议第一批直接带 `address_edit`。

## 9. Java 侧评审结论（2026-11-07，ItsMyGo）

| # | 决策点 | 结论 |
|---|---|---|
| 1 | 回传载体 | **同意方案 A**：`ConverseRequest` 新增 `FormResult form_result = 3;`（proto 无 part 结构，不复用）；一轮一条 Converse 的模型不变，票据/限流/审计/历史全部复用。方案 B 不采用。 |
| 2 | 字段值类型 | **统一 `map<string,string>`**（Java `Map<String,String>` 直接透传，agent 按 `form_type` 白名单解析）。约定：`number` 十进制字符串、`date` 用 `yyyy-MM-dd`、`select` 回传 option value；解析失败返回结构化表单错误。不用 `Struct`。 |
| 3 | `forms` 放哪 | **`FinalEvent` 顶层 + `HistoryMessage` 顶层**（与现有 `images/products/trace` 顶层 repeated 字段一致），不进 parts；旧客户端自动忽略未知字段。 |
| 4 | 历史回放 | **必须存**，但落点修正：历史由 **rag_helper Mongo** 存（Java `/api/agent/history` 直接映射 agent 返回，Java 无 `agent_history` 表）。做法：rag_helper history 文档存 `forms`，Java 只扩 `AgentHistoryMessage` DTO；历史里的表单**一律只读置灰**，只有本轮 SSE final 下发的可交互（无需给 FormRequest 加提交状态字段）。 |
| 5 | 过期策略 | **600s 够**（与行动确认卡一致）。双保险：前端到 `expires_in_seconds` 置灰；**Java 转发前**用 Redis `its-my-go:agent:form:{userId}:{formId}` 一次性消费（TTL = 有效期 + 60s 缓冲），SETNX 失败直接 SSE `error`（`FORM_EXPIRED` / `FORM_ALREADY_SUBMITTED`），不调 agent；历史回放旧 form_id 不参与消费。 |
| 6 | 多表单 | **同意 v0 最多 1 张**：proto 用 repeated 保留扩展，但服务端强制 `forms.length <= 1`（Java 只取第一张并告警，多余忽略）。 |
| 7 | 首批 form_type | **第一批：`refund_reason` + `address_edit`**（`address_edit` 无需新接口，见 §7 补充）；**第二批：`sku_select` / `order_pick`**。`refund_reason` 的 options **不由 rag_helper 硬编码**：由 Java 在 SSE 转发时用 `AfterSaleService.reasons()`（`GET /api/after-sales/reasons`）补全，保证原因字典单一口径。 |

**双方共同注意**：

1. proto 有两份：源头 `rag_helper/proto/agent.proto`，Java 仓副本 `ItsMyGo/src/main/proto/agent.proto`；
   改契约必须双端同步并重新生成 stub。
2. 灰度：`itsmygo.agent.forms.enabled=false` 默认关；关闭时 Java 既不下发 `forms` 也不接受
   `form_result`，避免 agent 先升级后被误消费。
3. Java 侧工作量按上表执行；评审稿 §6 中「Java 加历史表/列」一条作废（历史在 agent 侧）。

**结论**：方案 A 通过，v0 可按上述结论改 proto；表单工具与回传链路的实现待排期。

## 8. 验收用例（评审通过后落地）

1. 用户说「退款原因选一下」→ 前端渲染原因选择表单 → 提交 → agent 直接生成退款草稿，
   且草稿里的原因与所选一致（不靠自然语言猜）；
2. 同一 `form_id` 重复提交 → 第二次被拒（400/幂等提示）；
3. 旧前端（不渲染表单）→ 对话仍可继续，降级为文本追问；
4. 刷新页面后历史里的表单卡片可重新展示（只读，不可再提交）。

## 10. 落地记录（2026-11-07）

评审通过后双端已落地：

- **rag_helper**：`proto/agent.proto` 契约 + 表单下发/回传（`app/agent/forms.py`）+
  `refund_reason` / `address_edit` 两个工具 + 历史存档；回归 76/0。
- **ItsMyGo**：`0e4a34a`（协议接入）/ `c094fdf`（评审增量：Redis 一次性消费、售后字典
  选项补全与校验、`POST /internal/agent/actions/address`、前端表单卡片）。
- **联合栈真实票据验证 12/12**：见 `HANDOFF.md` §3.76 与
  `output/joint_form_e2e_20261107_174446.json`。
- 联调中修掉 rag_helper 侧两个 bug（表单收集器复位时序、表单回传缺票据上下文）。
