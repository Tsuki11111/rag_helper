"""
应用层 BM25 检索

为什么不用 Milvus 内置 BM25：当前库只有 610 条切片，应用层建索引足够快，
也不用改 collection schema、不用重新导入。等切片规模上来再考虑迁到 Milvus。

中文没有空格：这里用「单字 + 相邻二字」切词，型号/数字/英文按原 token 保留，
既不引入 jieba 依赖，也能让 `GS3104T-PRO`、`HAK 180` 这类精确词参与匹配。
索引按 collection 缓存，导入新文档后最多 5 分钟自动刷新；测试可手动 invalidate。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
import time
from typing import Any, Iterable

from app.clients.milvus_clients import get_milvus_client
from app.conf.milvus_config import milvus_config
from app.core.logger import logger

COLLECTION = milvus_config.chunks_collection
OUTPUT_FIELDS = ["chunk_id", "content", "title", "parent_title", "file_title", "item_name"]
MAX_DOCS = 16384
CACHE_TTL_SECONDS = 300

# 英文/型号 token 整体保留；中文连续段单独切
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*|[\u4e00-\u9fff]+")
_HTML_RE = re.compile(r"<[^>]+>")
_SPLIT_RE = re.compile(r"[._\-/]+")


def tokenize(text: str) -> list[str]:
    """把中文/英文混合文本切成 BM25 词项（小写、去 HTML 标签）"""
    text = _HTML_RE.sub(" ", text or "").lower()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        part = match.group()
        if "\u4e00" <= part[0] <= "\u9fff":
            tokens.extend(part)
            tokens.extend(part[i:i + 2] for i in range(len(part) - 1))
        else:
            tokens.append(part)
            pieces = [p for p in _SPLIT_RE.split(part) if p]
            if len(pieces) > 1:
                tokens.extend(pieces)
    return tokens


@dataclass
class _Doc:
    row: dict
    tokens: list[str]
    term_freq: Counter
    length: int


class BM25Index:
    """纯内存 BM25；构造后只读，可安全跨请求复用"""

    def __init__(self, rows: Iterable[dict], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs: list[_Doc] = []
        self.df: Counter = Counter()
        for row in rows:
            content = (row.get("content") or "").strip()
            title = (row.get("title") or "").strip()
            # 标题是章节语义的强信号；重复一次，避免正文里同词过多时被淹没
            tokens = tokenize(content) + tokenize(title) * 2
            if not tokens:
                continue
            self.docs.append(_Doc(row=row, tokens=tokens, term_freq=Counter(tokens), length=len(tokens)))
            self.df.update(set(tokens))
        self.n = len(self.docs)
        self.avgdl = sum(d.length for d in self.docs) / self.n if self.n else 0.0

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def _score(self, doc: _Doc, query_terms: list[str]) -> float:
        score = 0.0
        norm = self.k1 * (1.0 - self.b + self.b * doc.length / (self.avgdl or 1.0))
        for term in query_terms:
            tf = doc.term_freq.get(term, 0)
            if not tf:
                continue
            score += self._idf(term) * tf * (self.k1 + 1.0) / (tf + norm)
        return score

    def search(self, query: str, limit: int = 5, item_names: list[str] | None = None) -> list[dict]:
        """返回与向量检索同构的命中列表（外层 chunk_id + distance + entity）"""
        if not query or not self.docs:
            return []
        # 去重但保序：同一词在 query 里重复不应线性放大权重
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms:
            return []
        wanted = {str(x) for x in (item_names or []) if x}
        scored: list[tuple[float, _Doc]] = []
        for doc in self.docs:
            if wanted and str(doc.row.get("item_name") or "") not in wanted:
                continue
            score = self._score(doc, query_terms)
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits: list[dict] = []
        for score, doc in scored[:max(0, limit)]:
            entity = {field: doc.row.get(field) for field in OUTPUT_FIELDS}
            hits.append({"chunk_id": doc.row.get("chunk_id"), "distance": score, "entity": entity})
        return hits


@dataclass
class _CacheEntry:
    created_at: float
    row_count: int
    index: BM25Index


_CACHE: dict[str, _CacheEntry] = {}


def invalidate_cache() -> None:
    _CACHE.clear()


def _load_rows(client) -> tuple[list[dict], int]:
    stats = client.get_collection_stats(COLLECTION) or {}
    row_count = int(stats.get("row_count") or 0)
    rows = client.query(
        collection_name=COLLECTION,
        filter="",
        output_fields=OUTPUT_FIELDS,
        limit=MAX_DOCS,
    )
    return list(rows or []), row_count


def get_bm25_index(force: bool = False) -> BM25Index | None:
    """取（或重建）当前 collection 的 BM25 索引；Milvus 不可用时返回 None"""
    now = time.monotonic()
    cached = _CACHE.get(COLLECTION)
    if cached and not force and now - cached.created_at < CACHE_TTL_SECONDS:
        return cached.index
    client = get_milvus_client()
    if client is None:
        return None
    try:
        rows, row_count = _load_rows(client)
        index = BM25Index(rows)
        _CACHE[COLLECTION] = _CacheEntry(created_at=now, row_count=row_count, index=index)
        logger.info(f"[bm25] 索引构建完成：{index.n} 条切片，collection={COLLECTION}")
        return index
    except Exception as e:
        logger.warning(f"[bm25] 索引构建失败，回退为空：{type(e).__name__}: {e}")
        return None


def search_bm25(query: str, limit: int = 5, item_names: list[str] | None = None) -> list[dict]:
    """对 kb_chunks 做 BM25 检索；任何失败都返回空列表，不阻断检索链路"""
    index = get_bm25_index()
    if index is None:
        return []
    return index.search(query, limit=limit, item_names=item_names)


def _check_bm25_ranking() -> list:
    """离线自测：中文二字切词、型号保留、标题加权、产品过滤、空查询"""
    problems = []
    rows = [
        {"chunk_id": 1, "title": "3.4.1 装入全幅烫金膜盒", "content": "打开上盖，将烫金膜盒装入导轨。", "item_name": "Brother HAK 180 烫金机"},
        {"chunk_id": 2, "title": "1.2 产品简介", "content": "本机为桌面式烫金设备，支持 220V 电压。", "item_name": "Brother HAK 180 烫金机"},
        {"chunk_id": 3, "title": "3.4.2 装入半幅烫金膜盒", "content": "半幅膜盒的安装方式与全幅不同。", "item_name": "Brother HAK 180 烫金机"},
        {"chunk_id": 4, "title": "参数", "content": "GS3104T-PRO 机械键盘的参数。", "item_name": "GANSS GS3104T-PRO 机械键盘"},
    ]
    index = BM25Index(rows)
    hits = index.search("HAK 180 烫金膜盒怎么装入", limit=3)
    if not hits or hits[0]["chunk_id"] not in (1, 3):
        problems.append(f"BM25 未把膜盒章节排到前面：{[h['chunk_id'] for h in hits]}")
    filtered = index.search("GS3104T-PRO 参数", limit=5, item_names=["Brother HAK 180 烫金机"])
    if any(h["entity"]["item_name"] != "Brother HAK 180 烫金机" for h in filtered):
        problems.append("产品名过滤失效，混入了其他产品切片")
    if index.search("", limit=3):
        problems.append("空查询应返回空列表")
    if "gs3104t-pro" not in tokenize("GS3104T-PRO"):
        problems.append("型号 token 未保留")
    return problems


if __name__ == "__main__":
    logger.info("[测试] BM25 排序")
    issues = _check_bm25_ranking()
    if issues:
        for issue in issues:
            logger.error(f"[测试] [FAIL] {issue}")
    else:
        logger.success("[测试] [PASS] BM25 排序验证通过")
