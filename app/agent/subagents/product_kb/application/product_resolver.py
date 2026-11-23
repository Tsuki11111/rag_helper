"""
产品实体解析（产品目录 BM25 + 别名/型号/部件映射）

为什么单独一层：
- 产品名、型号、别名是低频、强区分度的词，适合 BM25/规则做**实体链接**；
- 文档证据是长文本语义匹配，适合 dense/HyDE；
- 之前让 LLM 从 catalog 里直接选 item_names，既不可审计，也会把
  “烫金膜盒”推断成某个具体产品。

本模块只回答“用户说的是哪个产品”，不负责找证据。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from app.clients.kb_cache import get_kb_cache_version
from app.clients.milvus_clients import get_milvus_client
from app.conf.milvus_config import milvus_config
from app.core.error_policy import degrade, degrade_dependency
from app.core.logger import logger
from app.lm.bm25_utils import tokenize
from app.utils.path_util import PROJECT_ROOT

CATALOG_PATH = PROJECT_ROOT / "data" / "product_catalog.json"
CACHE_SECONDS = 300
BM25_CANDIDATE_SCORE = 1.5


class ResolverUnavailable(RuntimeError):
    """产品目录依赖不可用（Milvus 未就绪），不是「目录里没有这个产品」"""


def empty_result(status: str = "none") -> dict:
    return {"status": status, "product_ids": [], "item_names": [],
            "candidates": [], "near": [], "variant_options": [], "needs_variant": False}

_NORM_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-z0-9]+(?:[._\-/][a-z0-9]+)*")
_VARIANT_ONLY_RE = re.compile(
    r"^(?:我?(?:用的|要|选|是))?\s*(?:全幅|半幅)(?:版|型号|的)*(?:[。.!！?？])?$")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return _NORM_RE.sub("", text)


def _has_model_token(query: str) -> bool:
    return any(len(t) >= 3 for t in _ASCII_RE.findall((query or "").casefold()))


@dataclass(frozen=True)
class ProductEntry:
    item_name: str
    file_title: str = ""
    aliases: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    model_names: tuple[str, ...] = ()
    manual_id: str = ""
    variants: tuple[str, ...] = ()
    category: str = ""

    @property
    def product_id(self) -> str:
        # 当前 Milvus 没有独立 product_id；标准名就是稳定主键。
        return self.item_name

    def search_text(self) -> str:
        return " ".join([
            self.item_name, *self.aliases, *self.models, *self.model_names,
            self.manual_id, self.category,
        ])


def _matched_model(query: str, entry: ProductEntry) -> str:
    """从用户原话里取出被点名的型号；同手册多型号时用于回答口径。"""
    qn = _norm(query)
    names = [*entry.model_names, *entry.models]
    for name in names:
        nn = _norm(name)
        if len(nn) >= 3 and nn in qn:
            return name
    return entry.model_names[0] if entry.model_names else (entry.models[0] if entry.models else entry.item_name)


class _BM25:
    """产品目录的轻量 BM25；目录通常只有几十到几百行，纯内存即可"""

    def __init__(self, entries: Iterable[ProductEntry], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.entries = list(entries)
        self._tokens: list[list[str]] = []
        self._df: dict[str, int] = {}
        for entry in self.entries:
            toks = tokenize(entry.search_text())
            self._tokens.append(toks)
            for term in set(toks):
                self._df[term] = self._df.get(term, 0) + 1
        self.n = len(self.entries)
        self.avgdl = sum(len(t) for t in self._tokens) / self.n if self.n else 0.0

    def _idf(self, term: str) -> float:
        import math
        df = self._df.get(term, 0)
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = 3) -> list[tuple[float, ProductEntry]]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or not self.entries:
            return []
        scored: list[tuple[float, ProductEntry]] = []
        for entry, toks in zip(self.entries, self._tokens):
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            norm = self.k1 * (1.0 - self.b + self.b * len(toks) / (self.avgdl or 1.0))
            score = 0.0
            for term in terms:
                freq = tf.get(term, 0)
                if freq:
                    score += self._idf(term) * freq * (self.k1 + 1.0) / (freq + norm)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit]


class ProductResolver:
    """产品实体解析：显式名/别名/型号/部件优先，BM25 只做候选补充与排序"""

    def __init__(self, entries: Iterable[ProductEntry], parts: dict[str, dict] | None = None):
        self.entries = list(entries)
        self.by_name = {e.item_name: e for e in self.entries}
        self.parts = parts or {}
        self._bm25 = _BM25(self.entries)

    def _confirmed(self, entry: ProductEntry, provenance: str,
                   variant: str = "", variant_options: list[str] | None = None,
                   target_model: str = "") -> dict:
        options = list(variant_options or [])
        needs_variant = bool(options) and not variant
        return {
            "status": "confirmed",
            "product_ids": [entry.product_id],
            "item_names": [entry.item_name],
            "manual_id": entry.manual_id or entry.file_title,
            "target_model": target_model or _matched_model("", entry),
            "model_names": list(entry.model_names),
            "candidates": [],
            "near": [],
            "provenance": provenance,
            "variant": variant,
            "variant_options": options,
            "needs_variant": needs_variant,
        }

    def _candidates(self, pairs: list[tuple[float, ProductEntry, str]]) -> dict:
        candidates = []
        seen = set()
        for score, entry, provenance in pairs:
            if entry.item_name in seen:
                continue
            seen.add(entry.item_name)
            candidates.append({
                "item_name": entry.item_name,
                "file_title": entry.file_title,
                "score": float(score),
                "near": False,
                "provenance": provenance,
            })
        return {
            "status": "candidates",
            "product_ids": [c["item_name"] for c in candidates],
            "item_names": [],
            "manual_id": "",
            "target_model": "",
            "model_names": [],
            "candidates": candidates,
            "near": [],
            "variant_options": [],
            "needs_variant": False,
        }

    @staticmethod
    def _variant_in_query(query: str, variants: Iterable[str]) -> str:
        qn = _norm(query)
        for variant in variants:
            if _norm(variant) and _norm(variant) in qn:
                return variant
        return ""

    def _part_hits(self, query: str) -> list[tuple[float, ProductEntry, dict]]:
        qn = _norm(query)
        hits: list[tuple[float, ProductEntry, dict]] = []
        for part_name, part in self.parts.items():
            if not _norm(part_name) or _norm(part_name) not in qn:
                continue
            entry = self.by_name.get(part.get("product") or "")
            if entry:
                hits.append((float(len(_norm(part_name))), entry, part))
        return hits

    def _explicit_hits(self, query: str) -> list[tuple[float, ProductEntry, str]]:
        qn = _norm(query)
        hits: dict[str, tuple[float, ProductEntry, str]] = {}
        for entry in self.entries:
            names = [entry.item_name, *entry.aliases]
            for name in names:
                nn = _norm(name)
                if len(nn) >= 3 and nn in qn:
                    old = hits.get(entry.item_name)
                    if old is None or len(nn) > old[0]:
                        hits[entry.item_name] = (float(len(nn)), entry, "alias")
            for model in entry.models:
                mn = _norm(model)
                if len(mn) >= 3 and mn in qn:
                    old = hits.get(entry.item_name)
                    if old is None or old[2] != "model":
                        hits[entry.item_name] = (float(len(mn)) + 10.0, entry, "model")
        return sorted(hits.values(), key=lambda x: x[0], reverse=True)

    def resolve(self, query: str, context: str = "") -> dict:
        query = (query or "").strip()
        if not query or not self.entries:
            return empty_result()
        # 上一轮问“烫金膜盒怎么安装”，这一轮只回“全幅/半幅”：
        # 仅对这种短规格回答拼接上下文，避免把无关上下文里的产品重新带进来。
        if context.strip() and _VARIANT_ONLY_RE.match(query):
            query = f"{query} {context.strip()}"

        # 先看显式点名：同一句里既有型号又有部件时，型号才是用户真正指定的产品，
        # 不能因为“烫金膜盒”就退回部件映射、再反问全幅/半幅。
        explicit = self._explicit_hits(query)
        if explicit:
            if len({e.item_name for _, e, _ in explicit}) == 1:
                entry = explicit[0][1]
                result = self._confirmed(
                    entry, explicit[0][2], "", [], _matched_model(query, entry),
                )
                logger.info(f"[product_resolver] 显式命中：{query!r} -> {entry.item_name} "
                            f"({explicit[0][2]})")
                return result
            return self._candidates(explicit)

        part_hits = self._part_hits(query)
        if part_hits:
            products = {e.item_name for _, e, _ in part_hits}
            if len(products) == 1:
                _, entry, part = part_hits[0]
                variants = list(part.get("variants") or entry.variants or [])
                variant = self._variant_in_query(query, variants)
                result = self._confirmed(
                    entry, "part", variant, variants, _matched_model(query, entry),
                )
                logger.info(f"[product_resolver] 部件命中：{query!r} -> {entry.item_name}，"
                            f"variant={variant!r}，needs_variant={result['needs_variant']}")
                return result
            return self._candidates([
                (score, entry, "part") for score, entry, _ in part_hits
            ])

        if not _has_model_token(query):
            logger.info(f"[product_resolver] 无显式产品：{query!r} -> none")
            return empty_result()

        bm25 = self._bm25.search(query, limit=3)
        if bm25 and bm25[0][0] >= BM25_CANDIDATE_SCORE:
            logger.info(f"[product_resolver] BM25 候选：{query!r} -> "
                        f"{[e.item_name for _, e in bm25]}")
            return self._candidates([(score, entry, "bm25") for score, entry in bm25])
        return empty_result()


_CACHE: dict[str, Any] = {"created_at": 0.0, "version": -1, "resolver": None}


def _load_config() -> dict:
    try:
        with CATALOG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        logger.warning(f"[product_resolver] 产品目录配置不存在：{CATALOG_PATH}")
        return {}
    except Exception as e:
        logger.warning(f"[product_resolver] 产品目录配置读取失败：{type(e).__name__}: {e}")
        return {}


def _load_entries(config: dict) -> list[ProductEntry]:
    client = get_milvus_client()
    if client is None:
        raise ResolverUnavailable("Milvus 不可用，无法读取产品目录")
    rows = client.query(
        collection_name=milvus_config.item_name_collection,
        filter="", output_fields=["item_name", "file_title"], limit=1000,
    )
    products_cfg = config.get("products") or {}
    entries = []
    for row in rows or []:
        name = (row.get("item_name") or "").strip()
        if not name:
            continue
        cfg = products_cfg.get(name) or {}
        models = tuple(str(x) for x in (cfg.get("models") or []) if x)
        model_names = tuple(str(x) for x in (cfg.get("model_names") or []) if x) or models
        entries.append(ProductEntry(
            item_name=name,
            file_title=row.get("file_title") or "",
            aliases=tuple(str(x) for x in (cfg.get("aliases") or []) if x),
            models=models,
            model_names=model_names,
            manual_id=str(cfg.get("manual_id") or row.get("file_title") or ""),
            variants=tuple(str(x) for x in (cfg.get("variants") or []) if x),
            category=str(cfg.get("category") or ""),
        ))
    return entries


def get_resolver(force: bool = False) -> ProductResolver | None:
    import time
    now = time.monotonic()
    version = get_kb_cache_version()
    cached = _CACHE.get("resolver")
    if (cached is not None and not force
            and (_CACHE.get("version") == version or version == 0)
            and now - _CACHE["created_at"] < CACHE_SECONDS):
        return cached
    config = _load_config()
    entries = _load_entries(config)
    if not entries:
        return None
    resolver = ProductResolver(entries, config.get("parts") or {})
    _CACHE["resolver"] = resolver
    _CACHE["created_at"] = now
    _CACHE["version"] = version
    logger.info(f"[product_resolver] 目录加载完成：{len(entries)} 个产品")
    return resolver


def resolve_product(query: str, context: str = "") -> dict:
    """对用户原话做产品实体解析。

    依赖不可用（Milvus 没起）和「目录里确实没有这个产品」必须分开：前者返回
    `status=unavailable` 让调用方给用户「服务暂时不可用」，不能当成 no_match 记成
    知识缺口；编程错误由 `degrade` 上抛，不静默吞掉。
    """
    try:
        resolver = get_resolver()
        if resolver is None:
            return empty_result()
        return resolver.resolve(query, context=context)
    except ResolverUnavailable as e:
        return degrade_dependency(
            "product_resolver", "产品实体解析", empty_result("unavailable"), str(e))
    except Exception as e:
        return degrade(
            "product_resolver", "产品实体解析", empty_result("unavailable"), e)


def _check_product_resolver() -> list:
    """离线自测：别名/型号/部件/变体/未知产品；不依赖 Milvus。"""
    hak = ProductEntry(
        "Brother HAK 180 烫金机", "hak180使用说明书",
        aliases=("HAK 180", "HAK180", "烫金机"), models=("hak180",),
        variants=("全幅", "半幅"), category="烫金机")
    z35 = ProductEntry(
        "联想 Z35 打印机", "z35使用说明书",
        aliases=("联想 Z35", "Z35"), models=("z35",), category="打印机")
    meter = ProductEntry(
        "万用表RS-12", "万用表使用说明",
        aliases=("万用表", "RS-12", "RS12"), models=("rs12",), category="万用表")
    hl = ProductEntry(
        "Brother HL-3040CN 网络打印机", "hl3040网络说明书",
        aliases=("HL-3040CN", "HL-3070CW", "3040CN", "3070CW"),
        models=("hl3040cn", "hl3070cw"),
        model_names=("HL-3040CN", "HL-3070CW"),
        manual_id="hl3040网络说明书", category="打印机")
    resolver = ProductResolver(
        [hak, z35, meter, hl],
        parts={"烫金膜盒": {"product": "Brother HAK 180 烫金机", "variants": ["全幅", "半幅"]}},
    )
    problems = []

    def expect(query: str, status: str, name: str = "", needs_variant: bool = False,
               context: str = "", target: str = ""):
        got = resolver.resolve(query, context=context)
        if got.get("status") != status:
            problems.append(f"{query!r} 期望 {status}，实得 {got.get('status')} ({got})")
            return
        if name and got.get("item_names") != [name]:
            problems.append(f"{query!r} 期望产品 {name}，实得 {got.get('item_names')}")
        if target and got.get("target_model") != target:
            problems.append(f"{query!r} 期望型号 {target}，实得 {got.get('target_model')}")
        if needs_variant and not got.get("needs_variant"):
            problems.append(f"{query!r} 应标记 needs_variant")

    expect("HAK180 怎么安装", "confirmed", hak.item_name)
    expect("联想 Z35 卡纸了", "confirmed", z35.item_name)
    expect("万用表怎么测电压", "confirmed", meter.item_name)
    expect("HL-3040CN 怎么设置操作面板", "confirmed", hl.item_name, target="HL-3040CN")
    expect("HL-3070CW 怎么设置操作面板", "confirmed", hl.item_name, target="HL-3070CW")
    expect("烫金膜盒怎么安装", "confirmed", hak.item_name, needs_variant=True)
    expect("全幅烫金膜盒怎么安装", "confirmed", hak.item_name)
    expect("全幅", "confirmed", hak.item_name, context="烫金膜盒怎么安装")
    for answer in ("是全幅的", "我要半幅", "我用的全幅"):
        got = resolver.resolve(answer, context="烫金膜盒怎么安装")
        if got.get("status") != "confirmed" or got.get("needs_variant"):
            problems.append(f"规格回答没有被识别为已确认：{answer!r} -> {got!r}")
        if not got.get("variant"):
            problems.append(f"规格回答没有提取 variant：{answer!r} -> {got!r}")
    expect("打印质量不清晰怎么办", "none")
    expect("小米15怎么开机", "none")
    expect("vivo手机怎么一键截屏", "none")
    return problems
