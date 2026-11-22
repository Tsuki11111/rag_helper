"""输入护栏配置。"""
import os

from dotenv import load_dotenv

load_dotenv()


class GuardConfig:
    def __init__(self):
        self.max_question_chars = int(os.getenv("MAX_QUESTION_CHARS") or 2000)


guard_config = GuardConfig()
