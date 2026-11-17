"""切片稳定身份与元数据字段。

旧 `kb_chunks` 只有 Milvus 自增 `chunk_id`，重建后 ID 全变，评测 gold、
增量更新和去重都无从谈起。这里用「文档 + 章节 + 章节内序号」生成稳定
`chunk_key`，并补齐文档/产品/章节/版本等可过滤字段。

注意：`chunk_id` 仅作为旧接口的兼容数值（仍然是 64 位整数），
新代码应优先使用 `chunk_key` 做稳定引用。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict

from app.conf.embedding_config import embedding_config


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_chunk_fields(
    chunk: Dict[str, Any],
    *,
    ordinal: int = 0,
    doc_version: str = "",
) -> Dict[str, Any]:
    """为一条切片生成 v2 schema 需要的稳定字段。

    `ordinal` 是同一章节内的切片序号，用来区分同章节的多个 chunk。
    调用方负责按 `(file_title, parent_title, part)` 递增传入。
    """
    file_title = _text(chunk.get("file_title"))
    item_name = _text(chunk.get("item_name"))
    parent_title = _text(chunk.get("parent_title")) or _text(chunk.get("title"))
    title = _text(chunk.get("title"))
    content = str(chunk.get("content") or "")
    part = int(chunk.get("part") or 0)

    doc_id = _sha1(file_title)[:16]
    section_id = _sha1(f"{doc_id}|{parent_title}|{part}")[:16]
    section_path = parent_title if parent_title == title else f"{parent_title} > {title}"
    content_hash = _sha1(content)[:32]

    # key 不包含 content_hash：正文修订后仍保持同一切片身份，便于 upsert。
    chunk_key = _sha1(f"{doc_id}|{section_id}|{ordinal}")[:32]
    legacy_chunk_id = chunk.get("chunk_id")
    try:
        chunk_id = int(legacy_chunk_id) if legacy_chunk_id not in (None, "") else int(chunk_key[:15], 16)
    except (TypeError, ValueError):
        chunk_id = int(chunk_key[:15], 16)

    metadata = chunk.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {
        **metadata,
        "legacy_file_title": file_title,
        "legacy_part": part,
    }

    return {
        "chunk_key": chunk_key,
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "doc_version": _text(chunk.get("doc_version")) or doc_version or "",
        "product_id": _text(chunk.get("product_id")) or item_name,
        "merchant_id": _text(chunk.get("merchant_id")),
        "section_id": section_id,
        "section_path": section_path[:1024],
        "section_index": part,
        "page_no": int(chunk.get("page_no") or 0),
        "chunk_index": ordinal,
        "content_hash": content_hash,
        "source_type": _text(chunk.get("source_type")) or "manual",
        "is_active": bool(chunk.get("is_active", True)),
        "embedding_model": _text(chunk.get("embedding_model")) or embedding_config.model,
        "metadata": metadata,
    }


def _check_chunk_identity() -> list:
    """回归：同一章节同一序号身份稳定，正文变化只改 content_hash。"""
    base = {
        "file_title": "某说明书",
        "parent_title": "3.4 安装",
        "title": "3.4.1 全幅",
        "content": "第一版正文",
        "part": 1,
    }
    first = build_chunk_fields(dict(base), ordinal=0)
    changed = build_chunk_fields(dict(base, content="第二版正文"), ordinal=0)
    next_chunk = build_chunk_fields(dict(base), ordinal=1)
    problems = []
    if first["chunk_key"] != changed["chunk_key"]:
        problems.append("正文变化不应改变 chunk_key")
    if first["content_hash"] == changed["content_hash"]:
        problems.append("正文变化应改变 content_hash")
    if first["chunk_key"] == next_chunk["chunk_key"]:
        problems.append("同章节不同序号应生成不同 chunk_key")
    if not isinstance(first["chunk_id"], int) or first["chunk_id"] <= 0:
        problems.append(f"chunk_id 不合法：{first['chunk_id']!r}")
    if first["doc_id"] == "" or first["section_id"] == "":
        problems.append("doc_id/section_id 不能为空")
    return problems
