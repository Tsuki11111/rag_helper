"""Embedding 客户端：只暴露 embed，测试可注入假实现。"""
from app.lm.embedding_utils import generate_embeddings


class DashScopeEmbeddingGateway:
    def embed(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        result = generate_embeddings(texts, text_type=text_type) or {}
        return result.get("dense") or []
