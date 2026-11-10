"""兼容旧启动路径：python -m app.import_process.api.file_import_service。"""
import uvicorn

from app.importing.api import app

if __name__ == "__main__":
    uvicorn.run(app=app, host="0.0.0.0", port=8001)
