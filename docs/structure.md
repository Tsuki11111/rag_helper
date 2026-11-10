# 代码与目录结构规范

> 目标：让「对话智能体 / 导入 ETL / 评测 / 基础设施」边界清晰，新人能按目录判断代码该放哪。
> 本文件是结构治理的权威说明；迁移期间旧路径通过兼容 shim 保留，禁止新代码引用旧路径。

## 1. 目标目录

```text
app/
  entrypoints/          # 进程入口，只做 app 组装与启动，不放业务
    agent_http.py       # FastAPI 8002
    agent_grpc.py       # gRPC 8003
    import_http.py      # FastAPI 8001
  agent/                # 对话智能体
    runtime/            # 主循环、流式、事件、表单、工具上下文
    subagents/
      product_kb/       # 产品知识库 RAG 图：graph.py / state.py / nodes/
      product_guide/    # 商品导购工具：tools.py / cards.py
      order_service/    # 订单售后：graph.py / state.py / nodes/ / tools/
      policy/           # 平台政策工具/子智能体
    tools/              # 仅主 agent 直接工具
  importing/            # 导入 ETL：graph.py / state.py / nodes/
  eval/                 # 评测：cases / judge / runner / store
  infrastructure/       # 外部依赖：clients/、config/
  shared/               # logger / retry / request_context / auth / sse / errors
scripts/                # 一次性运维脚本（argparse，不承载业务逻辑）
tests/
  unit/ integration/ regression/
docs/structure.md
```

## 2. 依赖方向

```text
entrypoints  →  agent / importing / eval  →  infrastructure / shared
```

- `entrypoints` 不写业务；业务模块不 import 入口模块。
- `infrastructure` 不 import `agent/importing`；`shared` 不依赖任何业务包。
- 子智能体之间不直接互相 import，跨域协作走主 agent 或公共工具。
- 兼容 shim 只允许「旧路径 → 新路径」单向转发，新增代码禁止引用旧路径。

## 3. 命名

- 模块文件 `snake_case.py`，单数名词；包用复数表示集合（`nodes/`、`tools/`、`clients/`）。
- 类 `PascalCase`，函数/变量 `snake_case`，常量 `UPPER_SNAKE_CASE`。
- 图节点统一 `node_<动作>.py`，导出函数 `node_<动作>(state)`。
- 一次性脚本 `scripts/<动词>_<对象>.py`，入口 `main()` + `argparse`。

## 4. 测试

- 所有测试放 `tests/`；生产模块不再新增 `_check_*`、`if __name__ == "__main__"` 演示。
- 纯函数用 `tests/unit/`，连真实依赖的用 `tests/integration/`，端到端回归用 `tests/regression/`。
- 迁移期间 `app/core/regression.py` 仍可加载旧 `_check_*`，但新增用例必须写进 `tests/`。

## 5. 配置与日志

- 环境变量只在 `app/conf/` 读取；业务代码只使用配置对象。
- 日志统一 `app/core/logger.py`（后续迁 `app/shared/logger.py`），不直接 `print`。
- 工具错误使用 `app/agent/runtime/tool_errors.py` 的 `ToolError`，API 边界统一转用户文案。

## 6. 迁移顺序

1. 阶段 0：ruff 配置、本文件、测试/脚本骨架（已完成）。
2. 阶段 1：一次性导入脚本移到 `scripts/`，删除生产模块演示入口。
3. 阶段 2：`app/import_process` → `app/importing`，保留旧路径 shim。
4. 阶段 3：FastAPI/gRPC 启动入口移到 `app/entrypoints`。
5. 阶段 4：`app/core/eval_*` → `app/eval`（已完成）；`app/clients`/`app/conf` → `app/infrastructure` 为后续项。
6. 阶段 5：测试迁移、回归 93 条、启动冒烟、文档与提交。

每阶段必须验证：`app.core.regression` 通过、`app.agent.runtime.main_agent` 可 import、三个服务可启动。
