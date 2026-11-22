"""管理端 Cookie 等认证配置。"""
import os

from dotenv import load_dotenv

load_dotenv()


class AuthConfig:
    def __init__(self):
        self.cookie_secure = os.getenv("COOKIE_SECURE") == "1"


auth_config = AuthConfig()
