"""买小购客服 HTTP 查询服务入口。"""
import os

import uvicorn

from app.agent.api.query_service import SERVICE_PORT, app


def main() -> None:
    host = os.getenv("SERVICE_HOST", "127.0.0.1")
    uvicorn.run(app=app, host=host, port=SERVICE_PORT)


if __name__ == "__main__":
    main()
