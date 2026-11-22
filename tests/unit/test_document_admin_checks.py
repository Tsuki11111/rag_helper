"""文档管理回归用例（从 app/utils/document_admin.py 迁出）。"""
import pytest

import app.utils.document_admin as _da

globals().update({k: v for k, v in vars(_da).items() if not k.startswith("__")})


def _check_local_artifact_bounds() -> List[str]:
    """撤回边界：文件名校验 + 定位/删除都不允许逃出 output/。"""
    import tempfile

    problems: List[str] = []
    orig_output = _da.OUTPUT_DIR
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "output"
        (base / "20260101" / "task").mkdir(parents=True)
        (base / "20260101" / "task" / "doc.md").write_text("x", encoding="utf-8")
        outside = Path(tmp) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        _da.OUTPUT_DIR = base
        try:
            for bad in ("..", "../outside", "a/b", "C:x"):
                try:
                    locate_local_artifacts(bad)
                    problems.append(f"越界文档名竟然通过：{bad!r}")
                except UnsafePathError:
                    pass
            targets = locate_local_artifacts("doc")
            if not any(p.endswith("doc.md") for p in targets):
                problems.append(f"正常文档定位失败：{targets!r}")
            report = _revoke_local([str(outside)])
            if not outside.exists():
                problems.append("越界删除竟然真的执行了")
            if report.get("ok"):
                problems.append("越界删除没有报错")
        finally:
            _da.OUTPUT_DIR = orig_output
    return problems


def _check_minio_prefix_defaults() -> List[str]:
    """回归：CI 没有 .env 时，MinIO 目录前缀也不能是 None（导入期 lstrip 会崩）。"""
    problems: List[str] = []
    if not minio_config.minio_img_dir:
        problems.append("MINIO_IMG_DIR 缺失时应有默认值（/upload-images）")
    if MINIO_IMAGE_PREFIX != "upload-images":
        problems.append(f"图片前缀解析不对：{MINIO_IMAGE_PREFIX!r}")
    return problems


def _check_item_name_repair_plan() -> List[str]:
    """
    回归（HANDOFF §4.26）：撤回时重建「被连带删除、但仍有其它文档在用」的产品名行。

    打桩 Milvus 与 embedding，不碰真实库；额外校验 `_revoke_milvus` 真的调用了修复逻辑
    （只测纯函数会漏掉「接线没接上」这种静默失效）。
    """
    from app.clients import milvus_clients as mc
    from app.lm import embedding_utils as eu

    problems: List[str] = []
    upserts: List[tuple] = []

    class _FakeChunks:
        def __init__(self, survivor):
            self.survivor = survivor
        def query(self, collection_name=None, filter=None, output_fields=None, limit=None):
            return self.survivor

    class _FakeRevoke:
        def __init__(self):
            self.deleted = []
        def query(self, collection_name=None, filter=None, output_fields=None, limit=None):
            return []
        def delete(self, collection_name=None, filter=None):
            self.deleted.append(collection_name)
        def flush(self, collection_name=None):
            pass

    orig_upsert = mc.upsert_item_name
    orig_embed = eu.generate_embeddings
    orig_get_client = _da.__dict__.get("get_milvus_client")
    orig_collect = _da.__dict__.get("_collect_item_name_victims")
    orig_repair = _da.__dict__.get("_repair_item_names")
    try:
        # 1) 有幸存文档 → 重建该行
        mc.upsert_item_name = lambda client, coll, name, title, vec: upserts.append((name, title))
        eu.generate_embeddings = lambda texts: {"dense": [[0.1, 0.2] for _ in texts]}
        repaired = _repair_item_names(
            _FakeChunks([{"file_title": "hak180使用说明书"}]),
            ["Brother HAK 180 烫金机"], "FAQ草稿")
        if len(repaired["repaired"]) != 1 or upserts != [("Brother HAK 180 烫金机", "hak180使用说明书")]:
            problems.append(f"应重建到 hak180使用说明书，实为 {repaired} / upserts={upserts}")

        # 2) 没有幸存文档 → 不重建（避免给已删文档造索引）
        upserts.clear()
        repaired = _repair_item_names(_FakeChunks([]), ["某个已无人用的产品"], "docB")
        if repaired["repaired"] or upserts:
            problems.append(f"无幸存文档时不该重建：{repaired} / {upserts}")

        # 3) embedding 失败 → 记错误但不抛（撤回不能因为修复失败整体失败）
        def _boom(texts):
            raise RuntimeError("embedding down")
        eu.generate_embeddings = _boom
        repaired = _repair_item_names(
            _FakeChunks([{"file_title": "hak180使用说明书"}]), ["Brother HAK 180 烫金机"], "FAQ草稿")
        if not repaired["errors"] or repaired["repaired"]:
            problems.append(f"embedding 失败应记错误且不重建：{repaired}")

        # 4) 接线：_revoke_milvus 必须调用修复（把本次文档的产品名行交给修复逻辑）
        called = []
        fake = _FakeRevoke()
        _da.get_milvus_client = lambda: fake
        _da._collect_item_name_victims = lambda client, expr: ["Brother HAK 180 烫金机"]
        _da._repair_item_names = lambda client, victims, title: called.append((victims, title)) or {"repaired": [], "errors": []}
        result = _revoke_milvus("FAQ草稿")
        if not called:
            problems.append("_revoke_milvus 没有调用产品名修复逻辑（接线丢失）")
        if not result.get("ok") or len(fake.deleted) != 2:
            problems.append(f"撤回删除流程异常：result={result} deleted={fake.deleted}")
    finally:
        mc.upsert_item_name = orig_upsert
        eu.generate_embeddings = orig_embed
        if orig_get_client is not None:
            _da.get_milvus_client = orig_get_client
        if orig_collect is not None:
            _da._collect_item_name_victims = orig_collect
        if orig_repair is not None:
            _da._repair_item_names = orig_repair
    return problems

CHECKS = [
    ("_check_local_artifact_bounds", _check_local_artifact_bounds),
    ("_check_minio_prefix_defaults", _check_minio_prefix_defaults),
    ("_check_item_name_repair_plan", _check_item_name_repair_plan),
]

@pytest.mark.parametrize("name,fn", CHECKS, ids=[n for n, _ in CHECKS])
def test_document_admin_pure(name, fn):
    assert fn() == []
