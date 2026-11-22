"""知识库导入 HTTP 服务入口。"""
import uvicorn

from app.conf.service_config import service_config
from app.importing.api import app


def main() -> None:
    uvicorn.run(app=app, host=service_config.host, port=service_config.import_port)


if __name__ == "__main__":
    main()
