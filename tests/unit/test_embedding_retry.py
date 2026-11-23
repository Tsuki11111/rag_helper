"""嵌入接口 429/5xx 必须进入重试，而不是直接失败。"""
import httpx

import app.lm.embedding_utils as eu


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "http://test/embedding")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response)

    def json(self):
        return self._body


class _Client:
    def __init__(self):
        self.calls = 0

    def post(self, endpoint, headers=None, json=None):
        self.calls += 1
        if self.calls == 1:
            return _Resp(429)
        return _Resp(200, {
            "usage": {"total_tokens": 3},
            "output": {"embeddings": [{"text_index": 0, "embedding": [0.1, 0.2]}]},
        })


def test_429_is_retried(monkeypatch):
    monkeypatch.setattr(eu, "_endpoint", lambda: "http://test/embedding")
    monkeypatch.setattr("app.core.retry.time.sleep", lambda seconds: None)
    client = _Client()

    dense, sparse, tokens = eu._batch_encode(client, ["你好"], "query", "dense")

    assert client.calls == 2
    assert dense == [[0.1, 0.2]]
    assert sparse == []
    assert tokens == 3
