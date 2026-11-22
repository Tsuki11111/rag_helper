"""真实依赖集成测试：Mongo / Redis / Milvus 在线时跑，不在时 SKIP。"""
import uuid

import pytest

from app.conf.storage_config import storage_config

pytestmark = pytest.mark.integration


def test_mongo_session_ownership(mongo_ready):
    from app.clients.mongo_history_clients import (
        ensure_session_owner,
        get_history_mongo_tool,
        get_session_owner,
    )

    sid = f"pytest_session_{uuid.uuid4().hex}"
    try:
        assert ensure_session_owner(sid, user_id="u1", tenant_id="t1") == "u1"
        assert get_session_owner(sid) == "u1"
        with pytest.raises(PermissionError):
            ensure_session_owner(sid, user_id="u2", tenant_id="t1")
    finally:
        tool = get_history_mongo_tool()
        tool.chat_message.delete_many({"session_id": sid})
        tool.chat_session.delete_many({"session_id": sid})


def test_redis_task_status_roundtrip(redis_ready):
    from app.utils.task_utils import (
        TASK_STATUS_PROCESSING,
        clear_task,
        get_task_status,
        update_task_status,
    )

    tid = f"pytest_task_{uuid.uuid4().hex}"
    try:
        update_task_status(tid, TASK_STATUS_PROCESSING)
        assert get_task_status(tid) == TASK_STATUS_PROCESSING
    finally:
        clear_task(tid)


def test_milvus_import_atomicity_on_temp_collection(milvus_ready):
    """真实 Milvus：先 upsert 新切片，再删陈旧 chunk_key；临时集合用完即删。"""
    from scripts.create_collections import build_chunks_schema, step3_build_index_params
    import app.importing.nodes.node_import_milvus as nim

    client = milvus_ready
    name = f"pytest_chunks_{uuid.uuid4().hex[:8]}"
    schema = build_chunks_schema(client, 4)
    client.create_collection(
        collection_name=name, schema=schema,
        index_params=step3_build_index_params(client, with_sparse=True))
    client.load_collection(name)

    old_row = {
        "chunk_key": "old-1", "chunk_id": 1, "file_title": "doc",
        "title": "安装", "parent_title": "安装", "section_path": "安装",
        "section_index": 0, "chunk_index": 0, "content": "旧正文",
        "item_name": "产品", "dense_vector": [0.1, 0.2, 0.3, 0.4],
        "sparse_vector": {0: 0.5},
    }
    old_row2 = dict(old_row, chunk_key="old-2", chunk_id=2, content="旧正文2")
    client.insert(collection_name=name, data=[old_row, old_row2])
    client.flush(name)

    chunks = [{
        "file_title": "doc", "title": "安装", "parent_title": "安装", "part": 0,
        "content": "新正文", "item_name": "产品", "dense_vector": [0.1, 0.2, 0.3, 0.4],
        "sparse_vector": {0: 0.5},
    }]
    old_name = nim.CHUNKS_COLLECTION_NAME
    nim.CHUNKS_COLLECTION_NAME = name
    try:
        old_keys = nim.step_3_collect_old_keys(client, chunks)
        written = nim.step_4_upsert_data(client, chunks)
        # 保留 old-1，old-2 应被当成陈旧键删除
        written[0]["chunk_key"] = "old-1"
        nim.step_5_delete_stale_chunks(client, written, old_keys)
        client.flush(name)
        rows = client.query(collection_name=name, filter='file_title == "doc"',
                            output_fields=["chunk_key"])
        keys = {r["chunk_key"] for r in rows}
        assert "old-1" in keys
        assert "old-2" not in keys
    finally:
        nim.CHUNKS_COLLECTION_NAME = old_name
        client.drop_collection(name)
