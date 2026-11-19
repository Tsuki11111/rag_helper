"""切片稳定身份与章节定位字段。

`kb_chunks` 的主键不再是 Milvus 自增 `chunk_id`：自增 id 在重建后全变，评测 gold、
增量更新和去重都无从谈起。这里用「文档 + 章节 + 章节内序号」生成稳定 `chunk_key`。

身份中间量（`doc_id`/`section_id`/`content_hash`）只在函数内计算，**不落盘**：
2026-11-17 精简 schema 时删掉了这些列，因为没有任何读方。

注意：`chunk_id` 仅作为旧接口的兼容数值（仍然是 64 位整数），
新代码应优先使用 `chunk_key` 做稳定引用。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict

# 本模块产出的字段：schema 精简后只剩这 5 个（其余字段来自解析结果）
GENERATED_FIELDS = ("chunk_key", "chunk_id", "section_path", "section_index", "chunk_index")


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_chunk_fields(chunk: Dict[str, Any], *, ordinal: int = 0) -> Dict[str, Any]:
    """为一条切片生成稳定身份与章节定位字段。

    `ordinal` 是同一章节内的切片序号，用来区分同章节的多个 chunk。
    调用方负责按 `(file_title, parent_title, part)` 递增传入。
    """
    file_title = _text(chunk.get("file_title"))
    parent_title = _text(chunk.get("parent_title")) or _text(chunk.get("title"))
    title = _text(chunk.get("title"))
    part = int(chunk.get("part") or 0)

    doc_id = _sha1(file_title)[:16]
    section_id = _sha1(f"{doc_id}|{parent_title}|{part}")[:16]
    # 优先用切分阶段维护的真实标题层级路径；旧数据/未提供时回退到 parent > title。
    # heading_path 不参与 chunk_key，重导只更新 section_path。
    heading_path = _text(chunk.get("heading_path"))
    section_path = heading_path or (
        parent_title if parent_title == title else f"{parent_title} > {title}")

    # key 不含正文：正文修订后仍保持同一切片身份，便于 upsert。
    chunk_key = _sha1(f"{doc_id}|{section_id}|{ordinal}")[:32]
    legacy_chunk_id = chunk.get("chunk_id")
    try:
        chunk_id = int(legacy_chunk_id) if legacy_chunk_id not in (None, "") else int(chunk_key[:15], 16)
    except (TypeError, ValueError):
        chunk_id = int(chunk_key[:15], 16)

    return {
        "chunk_key": chunk_key,
        "chunk_id": chunk_id,
        "section_path": section_path[:512],
        "section_index": part,
        "chunk_index": ordinal,
    }


def _check_chunk_identity() -> list:
    """回归：同一章节同一序号身份稳定；正文变化不改 key；只产出精简后的 5 个字段。"""
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
    with_legacy = build_chunk_fields(dict(base, chunk_id=12345), ordinal=0)
    with_path = build_chunk_fields(
        dict(base, heading_path="# 手册 > ## 3.4 安装 > ### 3.4.1 全幅"), ordinal=0)
    problems = []
    if first["chunk_key"] != changed["chunk_key"]:
        problems.append("正文变化不应改变 chunk_key")
    if first["chunk_key"] == next_chunk["chunk_key"]:
        problems.append("同章节不同序号应生成不同 chunk_key")
    if not isinstance(first["chunk_id"], int) or first["chunk_id"] <= 0:
        problems.append(f"chunk_id 不合法：{first['chunk_id']!r}")
    if with_legacy["chunk_id"] != 12345:
        problems.append(f"应复用旧 chunk_id：{with_legacy['chunk_id']!r}")
    if set(first) != set(GENERATED_FIELDS):
        problems.append(f"产出字段与精简 schema 不一致：{sorted(first)}")
    if first["section_path"] != "3.4 安装 > 3.4.1 全幅":
        problems.append(f"section_path 拼接不对：{first['section_path']!r}")
    if with_path["section_path"] != "# 手册 > ## 3.4 安装 > ### 3.4.1 全幅":
        problems.append(f"heading_path 没有优先于默认拼接：{with_path['section_path']!r}")
    if first["section_index"] != 1 or next_chunk["chunk_index"] != 1:
        problems.append(f"section_index/chunk_index 不对：{first['section_index']!r}/{next_chunk['chunk_index']!r}")
    return problems
