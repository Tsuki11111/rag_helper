"""买小购客服 HTTP 查询服务入口。"""
import uvicorn

from app.agent.api.query_service import app
from app.conf.service_config import service_config


def main() -> None:
    uvicorn.run(app=app, host=service_config.host, port=service_config.http_port)


if __name__ == "__main__":
    main()
