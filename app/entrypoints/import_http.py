"""知识库导入 HTTP 服务入口。"""
import os

import uvicorn

from app.importing.api import app


def main() -> None:
    host = os.getenv("SERVICE_HOST", "127.0.0.1")
    uvicorn.run(app=app, host=host, port=8001)


if __name__ == "__main__":
    main()
