# rag_helper 运行镜像：Python 3.12 + uv（依赖锁在 uv.lock）
#
# 三个服务共用一个镜像，靠 command 区分：
#   python -m app.agent.api.grpc_service                  # 8003 gRPC（Java 调用）
#   python -m app.agent.api.query_service                 # 8002 HTTP / SSE
#   python -m app.import_process.api.file_import_service  # 8001 知识库导入
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PROJECT_ROOT=/app

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /app

# 先只拷依赖清单，代码变动时不会重复装依赖
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY prompts ./prompts
COPY proto ./proto

# 运行期写入目录（compose 里会挂命名卷持久化）
RUN mkdir -p output logs

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

EXPOSE 8001 8002 8003

CMD ["python", "-m", "app.agent.api.grpc_service"]
