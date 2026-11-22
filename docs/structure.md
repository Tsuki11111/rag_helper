# 代码与目录结构规范

> 目标：让「对话智能体 / 导入 ETL / 评测 / 基础设施」边界清晰，新人能按目录判断代码该放哪。
> **本文是结构权威**；与 HANDOFF/README 冲突时以本文为准，状态与坑以 HANDOFF 为准。

## 1. 当前目录（2026-11-22）

```text
app/
  entrypoints/              # 进程入口，只做启动，不放业务
    agent_http.py           # 8002 HTTP/SSE
    agent_grpc.py           # 8003 gRPC
    import_http.py          # 8001 知识库导入
  agent/
    runtime/                # 主 agent 运行时（对用户说话的唯一出口）
      main_agent.py         # 组合根 + 一轮执行骨架 + 入口
      stream_consumer.py    # LangGraph 流 → 执行流事件；终稿提取
      answer_reconcile.py   # 配图/商品按最终回复裁剪与合并
      trace_labels.py       # 执行过程的中文标签（不暴露工具名/参数）
      grounding.py          # 终稿接地校验
      events.py / forms.py / stream_buffer.py / tool_context.py / tool_errors.py
    api/                    # 对外 HTTP/gRPC 适配（query / grpc / eval / proto）
    tools/                  # 仅主 agent 直接挂载的共享工具（report / tone_faq）
    subagents/
      product_kb/           # 产品知识库 RAG 图（图型特例）
        agent.py            # CompiledSubAgent 组合根
        application/        # graph / product_resolver / nodes
        domain/             # state / outcome
      product_guide/        # 商品导购
        agent.py  api/tools.py
        application/        # ProductGuideService / SemanticSearchService / Formatter
        domain/             # models / ports
        infrastructure/     # catalog / embedding / card / profile
      order_service/        # 订单售后
        agent.py  api/{query,actions,tools}.py   # 仅工具签名适配
        application/        # order_query_service / order_action_service / graphs / nodes
        domain/             # state / rules / ports
        infrastructure/     # 预留（共享客户端已上移 app/clients）
      policy_agent/         # 平台政策
        agent.py  api/tools.py
        application/        # PolicyService（只依赖端口）
        domain/             # ports
  clients/                  # 外部依赖适配（只做 IO，不依赖业务）
    its_mygo_client.py      # Java 内部接口（共享）
    policy_repository.py    # 政策 Milvus 集合（共享）
    milvus / mongo / redis / minio / neo4j / mcp ...
  conf/                     # 所有配置类；环境变量只在这里读取（启动/CLI 例外）
    lm / embedding / reranker / milvus / minio / pricing / pipeline / budget /
    product_guide / order_service / policy / tone_faq / storage / service / auth ...
  importing/                # 导入 ETL：graph / state / api / nodes
  import_process/           # 旧路径兼容 shim（只转发 app.importing.api）
  eval/                     # 评测：cases / judge / runner / case_store
  core/                     # 横切关注点：logger / usage / 护栏 / 回归 runner
  utils/                    # 历史工具（任务状态、SSE、路径、文档管理）——待迁
  lm/                       # LLM / embedding / rerank / BM25 客户端
scripts/                    # 一次性运维脚本（argparse / main）
prompts/                    # 所有提示词资产（*.prompt）
tests/regression/           # 现有回归用例（pytest 迁移待做）
```

## 2. 依赖方向

```text
entrypoints → agent / importing / eval → clients / conf / lm / core / utils
```

- `entrypoints` 不写业务；业务模块不 import 入口模块。
- `app/clients` 只做外部 IO，**不 import agent / importing / eval**。
- `app/conf` 不 import 业务；业务只使用配置对象，不直接读环境变量。
- **子智能体之间不直接互相 import**：跨域能力放 `app/clients` 或由主 agent 编排。
  - 例外：`product_kb` 是图型特例，节点直接依赖 `app/clients` / `app/lm`；
    `_rag_task` 因依赖本轮 ContextVar 仍留在 `runtime/main_agent.py`。
- 兼容 shim 只允许「旧路径 → 新路径」单向转发；新增代码禁止引用旧路径。

## 3. 分层约定

- **声明式 subagent**（product-guide / order-service / policy-agent）：
  `agent.py` 组合根 → `api/*` 只做模型工具适配 → `application/*` 业务服务 →
  `domain/*` DTO/state/Protocol 端口 → `infrastructure/*` 具体外部实现。
- **图型 subagent**（product-kb）：`application/graph|nodes` 是 LangGraph 直接调用的函数，
  `domain/state|outcome` 是状态与结局；不强行搬 Java 的 getter/setter/Factory。
- `api/tools.py` 只做参数收敛、默认值、错误转文案；业务规则不得写在这里。
- `application` 只依赖 `domain` 端口；具体实现由 `agent.py`/`api` 组合根注入。
- 服务端口配置在 `app/conf/service_config.py`；注意 `RAG_*_PORT` 是 compose 宿主机映射，
  不是进程内部监听端口。

## 4. 命名

- 模块文件 `snake_case.py`，单数名词；包用复数表示集合（`nodes/`、`tools/`、`clients/`）。
- 类 `PascalCase`，函数/变量 `snake_case`，常量 `UPPER_SNAKE_CASE`。
- 图节点统一 `node_<动作>.py`，导出 `node_<动作>(state)`。
- 一次性脚本 `scripts/<动词>_<对象>.py`，入口 `main()` + `argparse`。

## 5. 测试

- 现有回归：`app/core/regression.py` 显式注册，用例贴在守护代码旁（`_check_*`）。
- **新增用例优先写 `tests/`**；`pytest` 迁移与 CI 接入是待办（见 HANDOFF）。
- 依赖不可用（Mongo/Milvus/Redis）时 SKIP，不 FAIL；有真实依赖时应全跑。

## 6. 配置与日志

- 环境变量只在 `app/conf/` 读取。允许的例外：
  `app/core/logger.py`（引导期）、`app/utils/path_util.py`（找项目根）、
  CLI/自测入口（`mongo_user_clients` ensure、`grpc_client` ticket、Neo4j 自测）。
- 日志统一 `app/core/logger.py`；生产路径不直接 `print`。
- 工具错误使用 `app/agent/runtime/tool_errors.py` 的 `ToolError`，API 边界统一转用户文案。

## 7. 已完成的治理（2026-11-22）

- 所有提示词在 `prompts/`，代码通过 `load_prompt` 读取。
- 四个 subagent 统一 `agent/api/application/domain/infrastructure` 骨架；配置在 `app/conf`。
- `its_mygo_client` / `policy_repository` 已上移到 `app/clients`，切断子智能体互相 import。
- product-guide 的 `get_user_profile` 已移入 `product_guide/infrastructure`。
- order-service 的查询/动作业务已移入 `application`，`api` 只保留工具适配。
- policy-agent 的 `PolicyService` 只依赖 `domain/ports.PolicyRepositoryPort`。
- `main_agent` 已拆出 `stream_consumer` / `answer_reconcile` / `trace_labels`。
- 旧的 `docker/*.yml` 已删除，本地基础设施统一用 ItsMyGo `dev-infra`。

## 8. 待办（已识别，尚未迁）

1. `app/core` 与 `app/utils` 仍混有业务/基础设施：`task_utils`、`document_admin`、
   `knowledge_gaps`、`regression` 需要按 `shared/` / `infrastructure/` / `services/` 再拆。
2. `main_agent.py` 仍较大：turn runner、ContextVar、表单/内部事件、约 800 行自测待拆；
   自测迁 `tests/` 时一并处理。
3. `product_kb` 还没有 `domain/ports.py` / `infrastructure/`，节点直接依赖 Milvus/Redis；
   这是图型特例，迁移前先保持现状。
4. `pytest` 与 CI 尚未接入；`app/core/regression` 仍是唯一入口。
5. `app/import_process` shim、`output/reimport_*.py` 旧脚本待清理。
