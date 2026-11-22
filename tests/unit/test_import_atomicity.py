"""导入原子性：upsert 失败不删旧切片，成功后才删陈旧 key。"""
from app.importing.nodes.node_import_milvus import (
    step_3_collect_old_keys,
    step_4_upsert_data,
    step_5_delete_stale_chunks,
)


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.upserted = []
        self.deleted = []
        self.flushed = 0
        self.loaded = 0

    def query(self, **kwargs):
        return [{"chunk_key": "old-1"}, {"chunk_key": "old-2"}]

    def upsert(self, collection_name, data):
        if self.fail:
            raise RuntimeError("upsert boom")
        self.upserted.extend(data)
        return {"ids": [row["chunk_key"] for row in data]}

    def delete(self, collection_name, ids):
        self.deleted.extend(ids)

    def flush(self, collection_name):
        self.flushed += 1

    def load_collection(self, collection_name):
        self.loaded += 1


def _chunks():
    return [{
        "file_title": "doc", "title": "安装", "parent_title": "安装", "part": 0,
        "content": "正文", "item_name": "产品", "dense_vector": [0.1, 0.2],
        "sparse_vector": {0: 0.5},
    }]


def test_upsert_failure_keeps_old_chunks():
    client = FakeClient(fail=True)
    step_3_collect_old_keys(client, _chunks())
    try:
        step_4_upsert_data(client, _chunks())
        assert False, "upsert 失败应抛异常"
    except RuntimeError:
        pass
    assert client.deleted == []


def test_stale_keys_deleted_only_after_upsert():
    client = FakeClient()
    chunks = _chunks()
    old_keys = step_3_collect_old_keys(client, chunks)
    written = step_4_upsert_data(client, chunks)
    written[0]["chunk_key"] = "old-1"
    step_5_delete_stale_chunks(client, written, old_keys)
    assert client.deleted == ["old-2"]
    assert client.upserted
    assert client.flushed >= 1 and client.loaded >= 1
